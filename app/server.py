"""Servidor local: solo escucha en este ordenador."""
from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import os
import re
import shutil
import threading
import time
import traceback
import html
import uuid
from urllib.parse import quote
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__, calidad, edicion, epub_tr, estudio, migrate, pdf_tr, store, tools, updates
from .engines import ENGINE_INFO, LANGUAGES, EngineError, build_engine
from .pipeline import Cancelled, Translator, parse_glossary

MAX_UPLOAD = 800 * 1024 * 1024
INTERRUPTED = "La traducción se interrumpió al cerrar la app. Al reanudar no gastará cuota por lo ya traducido."
STAGES = {
    "preparando": (0, 4),
    "traduciendo": (4, 72),
    "maquetando": (72, 94),
    "guardando": (94, 97),
    "revisando": (97, 99),
}
STATIC = Path(__file__).parent / "static"
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}

app = FastAPI(title="Lumbre", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
memory: store.Memory | None = None
stop_flags: dict[str, threading.Event] = {}
study_flags: dict[str, threading.Event] = {}
study_jobs: dict[str, dict] = {}
running_lock = threading.Lock()
state_lock = threading.RLock()


life = {"latido": time.monotonic(), "visto": False, "reinicio": None}
data_ready = threading.Event()
data_ready.set()


def get_memory() -> store.Memory:
    global memory
    data_ready.wait()
    if memory is None:
        memory = store.Memory()
    return memory


@app.middleware("http")
async def only_this_computer(request: Request, call_next):
    host = (request.headers.get("host") or "").split(":")[0]
    origin = request.headers.get("origin")
    origin_host = re.sub(r"^https?://", "", origin or "").split(":")[0].split("/")[0]
    if host not in ALLOWED_HOSTS or (origin and origin_host not in ALLOWED_HOSTS):
        return JSONResponse({"detail": "Solo se admite este ordenador."}, status_code=403)
    return await call_next(request)


TRASH_DAYS = 30


def trash_dir() -> Path:
    return store.DATA / "papelera"


def _keep_awake(active: bool) -> None:
    """Evita que Windows suspenda el equipo durante una traducción larga."""
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001 if active else 0x80000000)
    except (AttributeError, OSError):
        pass


def _size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file()) if path.exists() else 0


def job_dir(job_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise HTTPException(404, "No existe ese libro.")
    path = store.JOBS / job_id
    if not path.is_dir():
        raise HTTPException(404, "No existe ese libro.")
    return path


def read_job(job_id: str) -> dict:
    # En Windows no se puede sustituir un archivo mientras otro hilo lo lee.
    with state_lock:
        return json.loads((job_dir(job_id) / "estado.json").read_text(encoding="utf-8"))


def write_job(job_id: str, **changes) -> dict:
    path = store.JOBS / job_id / "estado.json"
    with state_lock:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        data.update(changes)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
    return data


def recover_jobs() -> None:
    """Un trabajo no puede seguir en marcha si el programa acaba de arrancar."""
    if trash_dir().exists():
        limit = time.time() - TRASH_DAYS * 86400
        for item in trash_dir().iterdir():
            if item.stat().st_mtime < limit:
                shutil.rmtree(item, ignore_errors=True)
    if not store.JOBS.exists():
        return
    for directory in store.JOBS.iterdir():
        status = directory / "estado.json"
        try:
            job = json.loads(status.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if job.get("estado") in ("traduciendo", "en cola") and directory.name not in stop_flags:
            write_job(directory.name, estado="detenido", mensaje=INTERRUPTED, fase="")


def is_busy() -> bool:
    return bool(stop_flags or study_flags)


def read_paragraphs(job_id: str) -> list[dict]:
    directory = job_dir(job_id)
    translated = directory / "texto_traducido.json"
    source = translated if translated.exists() else directory / "texto_original.json"
    return json.loads(source.read_text(encoding="utf-8"))


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/estado")
def app_status():
    return {"version": __version__, "ocupado": is_busy(), "recuperando": not data_ready.is_set(), "recuperados": migrate.result.get("libros", 0)}


@app.post("/api/latido")
def heartbeat():
    life["latido"] = time.monotonic()
    life["visto"] = True
    return {"ok": True}


@app.post("/api/actualizar")
async def install_update(file: UploadFile = File(...)):
    if is_busy():
        raise HTTPException(409, "Espera a que termine la traducción en curso antes de actualizar.")
    temporary = store.DATA / "actualizacion_subida.zip"
    store.ensure_dirs()
    temporary.write_bytes(await file.read())
    try:
        version = updates.install(temporary, __version__)
    except updates.UpdateError as exc:
        raise HTTPException(400, str(exc))
    finally:
        temporary.unlink(missing_ok=True)
    can_restart = updates.schedule_restart()
    return {"version": version, "reinicia": can_restart}


@app.get("/api/ajustes")
def get_settings():
    config = store.load_config()
    info = []
    for engine in ENGINE_INFO:
        ready = {
            "azure": bool(config["azure_key"]), "deepl": bool(config["deepl_key"]),
            "claude": bool(config["claude_key"]), "libre": bool(config["libre_url"]), "prueba": True,
        }[engine["id"]]
        info.append({**engine, "listo": ready, "usado_mes": get_memory().usage(engine["id"])})
    return {"ajustes": store.public_config(config), "motores": info, "idiomas": LANGUAGES}


@app.post("/api/ajustes")
async def set_settings(request: Request):
    store.save_config(await request.json())
    return get_settings()


@app.get("/api/motores/{engine_id}/cuota")
def engine_quota(engine_id: str):
    try:
        return {"restante": build_engine(engine_id, store.load_config()).remaining_quota()}
    except (EngineError, KeyError, ValueError, TypeError):
        return {"restante": None}


@app.get("/api/actualizaciones")
def check_updates():
    found = updates.latest()
    if not found or not updates.is_newer(found["version"], __version__):
        return {"disponible": False, "version": __version__}
    return {"disponible": True, "version": found["version"], "notas": found["notas"], "actual": __version__}


@app.post("/api/actualizaciones/instalar")
def install_remote_update():
    if is_busy():
        raise HTTPException(409, "Espera a que termine la traducción en curso antes de actualizar.")
    found = updates.latest()
    if not found or not updates.is_newer(found["version"], __version__):
        raise HTTPException(400, "No hay ninguna versión nueva.")
    temporary = store.DATA / "actualizacion_descargada.zip"
    try:
        updates.download(found["url"], temporary)
        version = updates.install(temporary, __version__)
    except updates.UpdateError as exc:
        raise HTTPException(400, str(exc))
    finally:
        temporary.unlink(missing_ok=True)
    return {"version": version, "reinicia": updates.schedule_restart()}


@app.post("/api/motores/{engine_id}/probar")
def test_engine(engine_id: str):
    try:
        engine = build_engine(engine_id, store.load_config())
        sample = engine.check()
        remaining = engine.remaining_quota()
    except EngineError as exc:
        return JSONResponse({"ok": False, "mensaje": str(exc)}, status_code=200)
    except (KeyError, ValueError, TypeError):
        return JSONResponse({"ok": False, "mensaje": "El servicio respondió algo inesperado."}, status_code=200)
    return {"ok": True, "muestra": sample, "restante": remaining}


@app.get("/api/libros")
def list_jobs():
    jobs = []
    for directory in store.JOBS.iterdir() if store.JOBS.exists() else []:
        status = directory / "estado.json"
        if status.exists():
            try:
                with state_lock:
                    jobs.append(json.loads(status.read_text(encoding="utf-8")))
            except ValueError:
                continue
    return sorted(jobs, key=lambda job: job.get("creado", 0), reverse=True)


@app.post("/api/libros")
async def upload(file: UploadFile = File(...)):
    name = Path(file.filename or "libro").name
    suffix = Path(name).suffix.lower()
    if suffix not in (".epub", ".pdf"):
        raise HTTPException(400, "Solo se admiten archivos EPUB o PDF.")
    job_id = uuid.uuid4().hex
    directory = store.JOBS / job_id
    directory.mkdir(parents=True)
    destination = directory / f"original{suffix}"
    size = 0
    digest = hashlib.sha256()
    with destination.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
            if size > MAX_UPLOAD:
                out.close()
                shutil.rmtree(directory, ignore_errors=True)
                raise HTTPException(400, "El archivo supera los 800 MB.")
            out.write(chunk)
    fingerprint = digest.hexdigest()
    for other in list_jobs():
        if other.get("huella") == fingerprint:
            shutil.rmtree(directory, ignore_errors=True)
            return {**other, "duplicado": True}
    try:
        analyzer = epub_tr.analyze if suffix == ".epub" else pdf_tr.analyze
        info = await asyncio.to_thread(analyzer, destination)
    except (epub_tr.EpubError, pdf_tr.PdfError) as exc:
        shutil.rmtree(directory, ignore_errors=True)
        raise HTTPException(400, str(exc))
    except Exception as exc:
        shutil.rmtree(directory, ignore_errors=True)
        raise HTTPException(400, f"No se ha podido leer el libro ({type(exc).__name__}).")
    paragraphs = info.pop("parrafos")
    (directory / "texto_original.json").write_text(json.dumps(paragraphs, ensure_ascii=False), encoding="utf-8")
    terms = await asyncio.to_thread(tools.suggest_terms, paragraphs)
    return write_job(
        job_id, id=job_id, huella=fingerprint, archivo=name, creado=time.time(), estado="listo", progreso=0, hechos=0, total=info["caracteres"],
        mensaje="", terminos=terms, salidas={}, avisos_calidad=0, **info,
    )


@app.get("/api/libros/{job_id}")
def get_job(job_id: str):
    return read_job(job_id)


@app.delete("/api/libros/{job_id}")
def delete_job(job_id: str):
    directory = job_dir(job_id)
    if job_id in stop_flags:
        raise HTTPException(409, "Detén la traducción antes de borrar el libro.")
    trash_dir().mkdir(parents=True, exist_ok=True)
    target = trash_dir() / job_id
    shutil.rmtree(target, ignore_errors=True)
    shutil.move(str(directory), str(target))
    os.utime(target)
    return {"ok": True}


@app.post("/api/libros/{job_id}/restaurar")
def restore_job(job_id: str):
    if not re.fullmatch(r"[0-9a-f]{32}", job_id) or not (trash_dir() / job_id).is_dir():
        raise HTTPException(404, "Ese libro ya no está en la papelera.")
    store.JOBS.mkdir(parents=True, exist_ok=True)
    shutil.move(str(trash_dir() / job_id), str(store.JOBS / job_id))
    return read_job(job_id)


@app.get("/api/papelera")
def list_trash():
    books = []
    for item in trash_dir().iterdir() if trash_dir().exists() else []:
        try:
            job = json.loads((item / "estado.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        books.append({"id": job.get("id"), "titulo": job.get("titulo") or job.get("archivo"), "borrado": item.stat().st_mtime, "bytes": _size(item)})
    return sorted(books, key=lambda book: book["borrado"], reverse=True)


@app.delete("/api/papelera")
def empty_trash():
    shutil.rmtree(trash_dir(), ignore_errors=True)
    return {"ok": True}


@app.get("/api/almacenamiento")
def storage_info():
    return {"carpeta": str(store.DATA), "libros": _size(store.JOBS), "papelera": _size(trash_dir()), "memoria": _size(store.DATA / "memoria.sqlite")}


@app.post("/api/almacenamiento/abrir")
def open_data_folder():
    if os.name != "nt":
        raise HTTPException(400, "Solo disponible en Windows.")
    os.startfile(store.DATA)  # noqa: S606
    return {"ok": True}


@app.post("/api/libros/{job_id}/datos")
def edit_metadata(job_id: str, body: dict):
    read_job(job_id)
    changes = {key: str(body[key]).strip()[:300] for key in ("titulo", "autor") if key in body}
    if "titulo" in changes and not changes["titulo"]:
        raise HTTPException(400, "El título no puede quedar vacío.")
    return write_job(job_id, **changes)


@app.get("/api/libros/{job_id}/segmentos")
def list_segments(job_id: str, q: str = "", pagina: int = 0, desde: int = 0, limite: int = 40):
    segments = edicion.load(job_dir(job_id))
    needle = tools._fold(q.strip())
    found = []
    for index, segment in enumerate(segments):
        if pagina and edicion.page_of(segment["lugar"]) != pagina:
            continue
        editable = edicion.to_editable(segment["traduccion"])
        if needle and needle not in tools._fold(editable) and needle not in tools._fold(segment["original"]):
            continue
        found.append({"id": index, "lugar": segment["lugar"], "tipo": segment.get("tipo"), "original": segment["original"], "traduccion": editable, "editado": segment.get("editado", False)})
    return {"total": len(found), "segmentos": found[max(0, desde): max(0, desde) + min(limite, 100)], "pendientes": read_job(job_id).get("cambios_pendientes", False)}


def _store_segment(job_id: str, segment: dict, fragment: str) -> None:
    segment["traduccion"] = fragment
    memory_store = get_memory()
    memory_store.put_many([(segment["clave"], fragment), (segment["clave_general"], fragment)])
    write_job(job_id, cambios_pendientes=True)


@app.put("/api/libros/{job_id}/segmentos/{index}")
def update_segment(job_id: str, index: int, body: dict):
    directory = job_dir(job_id)
    segments = edicion.load(directory)
    if not 0 <= index < len(segments):
        raise HTTPException(404, "Ese párrafo no existe.")
    segment = segments[index]
    text = str(body.get("traduccion", "")).strip()
    if not text:
        raise HTTPException(400, "La traducción no puede quedar vacía.")
    if not edicion.valid_marks(edicion.to_editable(segment["traduccion"]), text):
        raise HTTPException(400, "Conserva las marcas ⟦ ⟧: guardan cursivas, enlaces y notas.")
    segment["editado"] = True
    _store_segment(job_id, segment, edicion.from_editable(text))
    edicion.save(directory, segments)
    return {"traduccion": edicion.to_editable(segment["traduccion"]), "avisos": calidad.check(segment["original"], edicion.plain_text(segment["traduccion"]), segment["lugar"], read_job(job_id).get("opciones", {}).get("origen", "auto"), read_job(job_id).get("destino", "es"), [])}


@app.post("/api/libros/{job_id}/segmentos/{index}/retraducir")
def retranslate_segment(job_id: str, index: int):
    directory = job_dir(job_id)
    job = read_job(job_id)
    segments = edicion.load(directory)
    if not 0 <= index < len(segments):
        raise HTTPException(404, "Ese párrafo no existe.")
    if job_id in stop_flags:
        raise HTTPException(409, "Espera a que termine la traducción en curso.")
    segment = segments[index]
    options = job.get("opciones", {})
    context = edicion.plain_text(segments[index - 1]["traduccion"])[-400:] if index else ""
    try:
        engine = build_engine(options.get("motor", "prueba"), store.load_config())
        result = engine.translate([segment["fragmento"]], options.get("origen", "auto"), options.get("destino", "es"), context)[0]
    except EngineError as exc:
        raise HTTPException(400, str(exc))
    segment["editado"] = True
    _store_segment(job_id, segment, result)
    edicion.save(directory, segments)
    get_memory().add_usage(options.get("motor", "prueba"), len(segment["original"]))
    return {"traduccion": edicion.to_editable(result)}


@app.post("/api/libros/{job_id}/reemplazar")
def replace_everywhere(job_id: str, body: dict):
    directory = job_dir(job_id)
    search, change = str(body.get("buscar", "")), str(body.get("cambiar", ""))
    if len(search.strip()) < 2:
        raise HTTPException(400, "Escribe al menos dos letras para buscar.")
    pattern = re.compile(r"(?<!\w)" + re.escape(search) + r"(?!\w)")
    segments = edicion.load(directory)
    if body.get("simular"):
        return {"coincidencias": sum(len(pattern.findall(edicion.plain_text(s["traduccion"]))) for s in segments)}
    changed = 0
    escaped = html.escape(change, quote=False)
    for segment in segments:
        parts = re.split(r"(<[^>]+>)", segment["traduccion"])
        updated = "".join(part if part.startswith("<") else pattern.sub(lambda _: escaped, part) for part in parts)
        if updated != segment["traduccion"]:
            changed += 1
            segment["editado"] = True
            _store_segment(job_id, segment, updated)
    edicion.save(directory, segments)
    return {"parrafos": changed}


@app.get("/api/libros/{job_id}/exportar/{kind}")
def export_book(job_id: str, kind: str):
    job = read_job(job_id)
    directory = job_dir(job_id)
    segments = edicion.load(directory)
    if not segments:
        raise HTTPException(404, "Vuelve a traducir el libro con esta versión para poder exportarlo.")
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", job.get("titulo") or Path(job["archivo"]).stem)[:120].strip() or "libro"
    if kind == "txt":
        data = edicion.export_txt(segments, job.get("titulo") or stem).encode("utf-8")
        return Response(data, media_type="text/plain; charset=utf-8", headers={"Content-Disposition": _attachment(f"{stem}.txt")})
    if kind == "epub":
        target = directory / "exportado.epub"
        edicion.export_epub(segments, target, job.get("titulo") or stem, job.get("autor") or "", job.get("destino", "es"), job_id)
        return FileResponse(target, filename=f"{stem}.epub", media_type="application/epub+zip")
    raise HTTPException(404, "Formato no disponible.")


def _attachment(name: str) -> str:
    return "attachment; filename*=UTF-8''" + quote(name)


@app.get("/api/libros/{job_id}/texto/{doc}/{number}")
def viewer_text(job_id: str, doc: str, number: int):
    path = viewer_file(job_id, doc)
    if read_job(job_id)["tipo"] == "pdf":
        with render_lock:
            document = pdf_tr.fitz.open(path)
            try:
                if not 1 <= number <= len(document):
                    raise HTTPException(404, "Esa página no existe.")
                return {"texto": document[number - 1].get_text()}
            finally:
                document.close()
    book = epub_tr.Book(path)
    try:
        if not 1 <= number <= len(book.documents):
            raise HTTPException(404, "Esa sección no existe.")
        root = epub_tr._parse(book.zip.read(book.documents[number - 1])).getroot()
        return {"texto": " ".join(" ".join(root.itertext()).split())}
    finally:
        book.zip.close()


def run_translation(job_id: str, options: dict) -> None:
    directory = store.JOBS / job_id
    stop = stop_flags[job_id]
    job = read_job(job_id)
    with running_lock:
        try:
            _keep_awake(True)
            write_job(job_id, estado="traduciendo", mensaje="", empezado=time.time(), fase="traduciendo", paso=0, pasos=0, progreso=0, cambios_pendientes=False)
            engine = build_engine(options["motor"], store.load_config())
            last_write = [0.0]

            def overall(stage: str, share: float) -> float:
                low, high = STAGES[stage]
                return round(low + (high - low) * min(1.0, share), 1)

            def progress(done: int, total: int) -> None:
                now = time.monotonic()
                if now - last_write[0] > 0.4 or done >= total:
                    last_write[0] = now
                    write_job(job_id, hechos=done, total=total, progreso=overall("traduciendo", done / max(1, total)))

            def stage(name: str, done: int, total: int) -> None:
                now = time.monotonic()
                if now - last_write[0] > 0.4 or done >= total or done == 0:
                    last_write[0] = now
                    write_job(job_id, fase=name, paso=done, pasos=total, progreso=overall(name, done / max(1, total)))

            translator = Translator(
                engine, get_memory(), options["origen"], options["destino"],
                parse_glossary(options.get("glosario", "")), progress, stop.is_set, stage,
                reuse_any=options.get("reanudar", False),
            )
            translator.ignore_memory = bool(options.get("sin_memoria"))
            if options.get("paginas"):
                translator.pages = set(edicion.parse_range(options["paginas"]))
            suffix = ".epub" if job["tipo"] == "epub" else ".pdf"
            output = directory / f"traducido{suffix}"
            bilingual = directory / f"bilingue{suffix}" if options.get("bilingue") else None
            module = epub_tr if job["tipo"] == "epub" else pdf_tr
            stats = module.translate(directory / f"original{suffix}", output, bilingual, translator, options["destino"])
            stage("revisando", 0, 1)
            if job["tipo"] == "pdf":
                translator.issues.extend(calidad.verify_pdf(output, lambda done, total: stage("revisando", done, total)))
            (directory / "informe.json").write_text(json.dumps(translator.issues, ensure_ascii=False), encoding="utf-8")
            edicion.save(directory, translator.segments)
            stage("revisando", 0, 1)
            try:
                save_translated_text(directory, job["tipo"], output)
            except Exception as exc:
                # El libro ya está guardado; solo faltaría el texto para consultas.
                print(f"Texto traducido no disponible: {exc}", flush=True)
            outputs = {"traducido": output.name}
            if bilingual:
                outputs["bilingue"] = bilingual.name
            write_job(
                job_id, estado="terminado", fase="", progreso=100, salidas=outputs, avisos_calidad=len(translator.issues),
                calidad=calidad.summary(translator.issues),
                estadisticas=stats, enviados=translator.sent, reutilizados=translator.reused, motor=options["motor"],
                destino=options["destino"], terminado=time.time(), mensaje="", parcial=options.get("paginas", ""),
            )
        except Cancelled:
            write_job(job_id, estado="detenido", fase="", mensaje="Traducción detenida. Lo hecho queda guardado para reanudar.")
        except EngineError as exc:
            write_job(job_id, estado="pausado", fase="", tipo_error=exc.kind, mensaje=str(exc))
        except Exception as exc:
            traceback.print_exc()
            write_job(job_id, estado="error", fase="", mensaje=f"Error inesperado: {type(exc).__name__}: {exc}")
        finally:
            _keep_awake(False)
            stop_flags.pop(job_id, None)


def save_translated_text(directory: Path, kind: str, output: Path) -> None:
    """Conserva el texto ya traducido para resumir y consultar sin gastar más cuota."""
    paragraphs = (epub_tr if kind == "epub" else pdf_tr).analyze(output)["parrafos"]
    (directory / "texto_traducido.json").write_text(json.dumps(paragraphs, ensure_ascii=False), encoding="utf-8")


@app.post("/api/libros/{job_id}/traducir")
async def start_translation(job_id: str, request: Request):
    job = read_job(job_id)
    if job.get("bloqueado"):
        raise HTTPException(400, "Este PDF no tiene texto seleccionable; esta versión aún no incluye OCR.")
    if job_id in stop_flags:
        raise HTTPException(409, "Este libro ya se está traduciendo.")
    body = await request.json()
    options = {
        "motor": body.get("motor", "prueba"), "origen": body.get("origen", "auto"), "destino": body.get("destino", "es"),
        "bilingue": bool(body.get("bilingue")), "glosario": body.get("glosario", ""),
        "reanudar": job.get("estado") in ("pausado", "detenido", "error") or bool(body.get("reanudar")),
        "sin_memoria": bool(body.get("sin_memoria")),
        "paginas": str(body.get("paginas", "")).strip(),
    }
    if options["paginas"]:
        try:
            if not edicion.parse_range(options["paginas"], job.get("unidades")):
                raise ValueError
        except ValueError:
            raise HTTPException(400, f"No entiendo esas páginas. Escríbelas así: 40-45, 50 (máximo {job.get('unidades')}).")
    general = store.load_config().get("glosario_general", "")
    if body.get("reconstruir"):
        options["glosario"] = job.get("opciones", {}).get("glosario", options["glosario"])
        options["reanudar"] = True
    elif general:
        options["glosario"] = "\n".join(part for part in (general, options["glosario"]) if part)
    options["glosario_libro"] = job.get("opciones", {}).get("glosario_libro", "") if body.get("reconstruir") else body.get("glosario", "")
    if options["reanudar"]:
        options["sin_memoria"] = False
    if options["origen"] not in LANGUAGES or options["destino"] not in LANGUAGES or options["destino"] == "auto":
        raise HTTPException(400, "Idioma no admitido.")
    try:
        build_engine(options["motor"], store.load_config())
    except EngineError as exc:
        raise HTTPException(400, str(exc))
    stop_flags[job_id] = threading.Event()
    write_job(job_id, estado="en cola", mensaje="", opciones=options, motor=options["motor"])
    threading.Thread(target=run_translation, args=(job_id, options), daemon=True).start()
    return read_job(job_id)


@app.post("/api/libros/{job_id}/detener")
def stop_translation(job_id: str):
    job_dir(job_id)
    flag = stop_flags.get(job_id)
    if flag:
        flag.set()
    return {"ok": True}


EPUB_TYPES = {".xhtml": "application/xhtml+xml", ".html": "text/html", ".htm": "text/html", ".css": "text/css", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".svg": "image/svg+xml", ".webp": "image/webp", ".ttf": "font/ttf", ".otf": "font/otf", ".woff": "font/woff", ".woff2": "font/woff2"}
VIEWER_CSP = "default-src 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; font-src 'self'; sandbox allow-same-origin"
render_lock = threading.Lock()


def viewer_file(job_id: str, doc: str) -> Path:
    job = read_job(job_id)
    suffix = ".epub" if job["tipo"] == "epub" else ".pdf"
    names = {"original": f"original{suffix}", "traducido": job.get("salidas", {}).get("traducido"), "bilingue": job.get("salidas", {}).get("bilingue")}
    name = names.get(doc)
    if not name:
        raise HTTPException(404, "Ese archivo no existe.")
    return job_dir(job_id) / name


@app.get("/api/libros/{job_id}/visor")
def viewer_info(job_id: str):
    job = read_job(job_id)
    docs = {}
    for doc in ("original", "traducido", "bilingue"):
        try:
            path = viewer_file(job_id, doc)
        except HTTPException:
            continue
        if not path.exists():
            continue
        if job["tipo"] == "pdf":
            with render_lock:
                document = pdf_tr.fitz.open(path)
                docs[doc] = {"paginas": len(document)}
                document.close()
        else:
            book = epub_tr.Book(path)
            docs[doc] = {"paginas": len(book.documents), "secciones": book.documents}
            book.zip.close()
    return {"tipo": job["tipo"], "documentos": docs}


@app.get("/api/libros/{job_id}/pagina/{doc}/{number}.jpg")
def viewer_page(job_id: str, doc: str, number: int, ancho: int = 900):
    path = viewer_file(job_id, doc)
    width = min((600, 900, 1200, 1600), key=lambda option: abs(option - ancho))
    cache = job_dir(job_id) / "visor" / doc
    target = cache / f"{number}-{width}.jpg"
    if not target.exists() or target.stat().st_mtime < path.stat().st_mtime:
        cache.mkdir(parents=True, exist_ok=True)
        with render_lock:
            document = pdf_tr.fitz.open(path)
            try:
                if not 1 <= number <= len(document):
                    raise HTTPException(404, "Esa página no existe.")
                page = document[number - 1]
                pix = page.get_pixmap(matrix=pdf_tr.fitz.Matrix(width / page.rect.width, width / page.rect.width), alpha=False)
                target.write_bytes(pix.tobytes("jpeg", jpg_quality=86))
            finally:
                document.close()
    return FileResponse(target, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})


@app.get("/api/libros/{job_id}/epub/{doc}/{route:path}")
def viewer_epub(job_id: str, doc: str, route: str):
    path = viewer_file(job_id, doc)
    with zipfile.ZipFile(path) as archive:
        if route not in archive.namelist():
            raise HTTPException(404, "Ese archivo no existe.")
        data = archive.read(route)
    media = EPUB_TYPES.get(Path(route).suffix.lower(), "application/octet-stream")
    return Response(data, media_type=media, headers={"Content-Security-Policy": VIEWER_CSP, "X-Content-Type-Options": "nosniff"})


@app.get("/api/libros/{job_id}/buscar")
def viewer_search(job_id: str, q: str = ""):
    needle = tools._fold(q.strip())
    if len(needle) < 2:
        return []
    directory = job_dir(job_id)
    results = []
    for doc, name in (("traducido", "texto_traducido.json"), ("original", "texto_original.json")):
        source = directory / name
        if not source.exists():
            continue
        for entry in json.loads(source.read_text(encoding="utf-8")):
            folded = tools._fold(entry["texto"])
            at = folded.find(needle)
            if at >= 0:
                results.append({"documento": doc, "lugar": entry["lugar"], "texto": entry["texto"][max(0, at - 70): at + 130]})
                if len(results) >= 60:
                    return results
    return results


@app.get("/api/libros/{job_id}/informe")
def get_report(job_id: str):
    path = job_dir(job_id) / "informe.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


@app.get("/api/libros/{job_id}/informe.csv")
def get_report_csv(job_id: str):
    stream = io.StringIO()
    writer = csv.writer(stream, delimiter=";")
    writer.writerow(["Gravedad", "Tipo", "Lugar", "Aviso", "Original", "Traducción"])
    for issue in get_report(job_id):
        writer.writerow([issue.get("gravedad", "media"), issue["tipo"], issue["lugar"], issue["mensaje"], issue["original"], issue["traduccion"]])
    return PlainTextResponse(
        "﻿" + stream.getvalue(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="informe_de_calidad.csv"'},
    )


@app.get("/api/libros/{job_id}/descargar/{kind}")
def download(job_id: str, kind: str):
    job = read_job(job_id)
    name = job.get("salidas", {}).get(kind)
    if not name:
        raise HTTPException(404, "Ese archivo no existe.")
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", Path(job["archivo"]).stem)[:150].strip() or "libro"
    language = LANGUAGES.get(job.get("destino", "es"), "traducido").lower()
    label = "bilingüe" if kind == "bilingue" else language
    return FileResponse(job_dir(job_id) / name, filename=f"{stem} ({label}){Path(name).suffix}")


def run_study(job_id: str, metodo: str, modo: str) -> None:
    directory = job_dir(job_id)
    state = study_jobs[job_id]
    stop = study_flags[job_id]
    try:
        paragraphs = read_paragraphs(job_id)
        if metodo == "ia":
            provider = estudio.provider_config(store.load_config())
            if not provider:
                raise EngineError("auth", "Elige una IA en Ajustes.")

            def progress(done: int, total: int) -> None:
                state.update(hecho=done, total=total)

            pack = estudio.generate_ai(paragraphs, provider, directory / "estudio_bloques", modo, progress, stop.is_set)
        else:
            pack = estudio.generate_local(paragraphs, edicion.load(directory) if (directory / "segmentos.json").exists() else None)
        pack["fuente"] = "traducido" if (directory / "texto_traducido.json").exists() else "original"
        if not any(pack["temas"]):
            raise EngineError("bad", "El libro no tiene texto suficiente.")
        temporary = directory / "estudio.json.tmp"
        temporary.write_text(json.dumps(pack, ensure_ascii=False), encoding="utf-8")
        temporary.replace(directory / "estudio.json")
        state.update(estado="listo", mensaje="")
    except EngineError as exc:
        state.update(estado="detenido" if exc.kind == "stop" else "error", mensaje="" if exc.kind == "stop" else str(exc))
    except Exception as exc:
        traceback.print_exc()
        state.update(estado="error", mensaje=f"No se pudo preparar el material: {exc}")
    finally:
        study_flags.pop(job_id, None)


@app.get("/api/libros/{job_id}/estudio")
def study_status(job_id: str):
    directory = job_dir(job_id)
    if job_id in study_flags:
        return {"material": None, "progreso": {}, "trabajo": study_jobs[job_id], "ia": None, "estimacion": {}, "traducido": False}
    path = directory / "estudio.json"
    progress_path = directory / "estudio_progreso.json"
    paragraphs = read_paragraphs(job_id)
    config = store.load_config()
    provider = estudio.provider_config(config)
    return {
        "material": json.loads(path.read_text(encoding="utf-8")) if path.exists() else None,
        "progreso": json.loads(progress_path.read_text(encoding="utf-8")) if progress_path.exists() else {},
        "trabajo": study_jobs.get(job_id) or {"estado": "parado"},
        "ia": {"id": provider["id"], "nombre": provider["nombre"], "modelo": provider["modelo"]} if provider else None,
        "estimacion": {modo: estudio.estimate(paragraphs, modo) for modo in ("expres", "completo")},
        "traducido": (directory / "texto_traducido.json").exists(),
    }


@app.post("/api/libros/{job_id}/estudio")
def study_start(job_id: str, body: dict):
    job_dir(job_id)
    if job_id in study_flags:
        raise HTTPException(409, "Ya se está preparando el material de este libro.")
    metodo = "ia" if body.get("metodo") == "ia" else "local"
    modo = "completo" if body.get("modo") == "completo" else "expres"
    study_flags[job_id] = threading.Event()
    study_jobs[job_id] = {"estado": "preparando", "metodo": metodo, "hecho": 0, "total": 0, "mensaje": ""}
    threading.Thread(target=run_study, args=(job_id, metodo, modo), daemon=True).start()
    return study_jobs[job_id]


@app.post("/api/libros/{job_id}/estudio/detener")
def study_stop(job_id: str):
    flag = study_flags.get(job_id)
    if flag:
        flag.set()
    return {"ok": True}


@app.put("/api/libros/{job_id}/estudio/progreso")
def study_progress(job_id: str, body: dict):
    payload = json.dumps(body, ensure_ascii=False)
    if len(payload) > 2_000_000:
        raise HTTPException(413, "Progreso demasiado grande.")
    (job_dir(job_id) / "estudio_progreso.json").write_text(payload, encoding="utf-8")
    return {"ok": True}


@app.get("/api/libros/{job_id}/estudio/exportar/{formato}")
def study_export(job_id: str, formato: str):
    path = job_dir(job_id) / "estudio.json"
    if not path.exists():
        raise HTTPException(404, "Todavía no hay material de estudio.")
    pack = json.loads(path.read_text(encoding="utf-8"))
    title = read_job(job_id).get("titulo") or "Libro"
    safe = re.sub(r'[^\w -]', "", title)[:60].strip() or "Libro"
    if formato == "anki":
        content, media, name = estudio.export_anki(pack), "text/plain; charset=utf-8", f"{safe} - tarjetas Anki.txt"
    elif formato == "md":
        content, media, name = estudio.export_markdown(pack, title), "text/markdown; charset=utf-8", f"{safe} - estudio.md"
    else:
        raise HTTPException(404, "Formato desconocido.")
    return Response(content, media_type=media, headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


@app.post("/api/ia/probar")
def test_ai():
    provider = estudio.provider_config(store.load_config())
    if not provider:
        return {"ok": False, "mensaje": "Elige primero una IA."}
    try:
        return {"ok": True, "muestra": estudio.chat(provider, "Responde solo con la palabra: funciona", as_json=False, max_tokens=200).strip()[:80]}
    except EngineError as exc:
        return {"ok": False, "mensaje": str(exc)}


@app.post("/api/libros/{job_id}/preguntar")
def ask(job_id: str, body: dict):
    question = str(body.get("pregunta", "")).strip()[:1000]
    if not question:
        raise HTTPException(400, "Escribe una pregunta.")
    passages = tools.retrieve(read_paragraphs(job_id), question)
    if not passages:
        return {"respuesta": "", "pasajes": []}
    provider = estudio.provider_config(store.load_config())
    if body.get("metodo") == "ia" and provider:
        try:
            return {"respuesta": estudio.answer_question(question, passages, provider), "pasajes": passages}
        except EngineError as exc:
            raise HTTPException(400, str(exc))
    return {"respuesta": "", "pasajes": passages}
