"""Servidor local: solo escucha en este ordenador."""
from __future__ import annotations

import asyncio
import csv
import io
import json
import re
import shutil
import threading
import time
import traceback
import uuid
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__, calidad, epub_tr, migrate, pdf_tr, store, tools, updates
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
    return bool(stop_flags)


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
    with destination.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD:
                out.close()
                shutil.rmtree(directory, ignore_errors=True)
                raise HTTPException(400, "El archivo supera los 800 MB.")
            out.write(chunk)
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
        job_id, id=job_id, archivo=name, creado=time.time(), estado="listo", progreso=0, hechos=0, total=info["caracteres"],
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
    shutil.rmtree(directory)
    return {"ok": True}


def run_translation(job_id: str, options: dict) -> None:
    directory = store.JOBS / job_id
    stop = stop_flags[job_id]
    job = read_job(job_id)
    with running_lock:
        try:
            write_job(job_id, estado="traduciendo", mensaje="", empezado=time.time(), fase="traduciendo", paso=0, pasos=0, progreso=0)
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
            suffix = ".epub" if job["tipo"] == "epub" else ".pdf"
            output = directory / f"traducido{suffix}"
            bilingual = directory / f"bilingue{suffix}" if options.get("bilingue") else None
            module = epub_tr if job["tipo"] == "epub" else pdf_tr
            stats = module.translate(directory / f"original{suffix}", output, bilingual, translator, options["destino"])
            stage("revisando", 0, 1)
            if job["tipo"] == "pdf":
                translator.issues.extend(calidad.verify_pdf(output, lambda done, total: stage("revisando", done, total)))
            (directory / "informe.json").write_text(json.dumps(translator.issues, ensure_ascii=False), encoding="utf-8")
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
                destino=options["destino"], terminado=time.time(), mensaje="",
            )
        except Cancelled:
            write_job(job_id, estado="detenido", fase="", mensaje="Traducción detenida. Lo hecho queda guardado para reanudar.")
        except EngineError as exc:
            write_job(job_id, estado="pausado", fase="", tipo_error=exc.kind, mensaje=str(exc))
        except Exception as exc:
            traceback.print_exc()
            write_job(job_id, estado="error", fase="", mensaje=f"Error inesperado: {type(exc).__name__}: {exc}")
        finally:
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
    }
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


@app.post("/api/libros/{job_id}/resumen")
def summarize(job_id: str, body: dict):
    level = body.get("nivel", "media")
    paragraphs = read_paragraphs(job_id)
    config = store.load_config()
    if body.get("metodo") == "claude":
        if not config["claude_key"]:
            raise HTTPException(400, "Falta la clave de la API de Claude. Añádela en Ajustes.")
        try:
            text = tools.claude_summary(paragraphs, level, config["claude_key"], config["claude_summary_model"])
        except EngineError as exc:
            raise HTTPException(400, str(exc))
        result = {"metodo": "claude", "markdown": text}
    else:
        count = tools.LEVELS.get(level, tools.LEVELS["media"])[1]
        result = {"metodo": "frases", "frases": tools.extractive_summary(paragraphs, count)}
    (job_dir(job_id) / f"resumen_{level}.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result


@app.post("/api/libros/{job_id}/preguntar")
def ask(job_id: str, body: dict):
    question = str(body.get("pregunta", "")).strip()[:1000]
    if not question:
        raise HTTPException(400, "Escribe una pregunta.")
    passages = tools.retrieve(read_paragraphs(job_id), question)
    config = store.load_config()
    if not passages:
        return {"respuesta": "", "pasajes": []}
    if body.get("metodo") == "claude" and config["claude_key"]:
        try:
            answer = tools.claude_answer(question, passages, config["claude_key"], config["claude_summary_model"])
        except EngineError as exc:
            raise HTTPException(400, str(exc))
        return {"respuesta": answer, "pasajes": passages}
    return {"respuesta": "", "pasajes": passages}
