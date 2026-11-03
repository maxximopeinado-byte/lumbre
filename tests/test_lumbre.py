"""Comprueba libros, motores (contra servidores simulados) y la API local."""
import json
import os
import re
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pymupdf as fitz
import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
from fixtures import make_epub, make_pdf, PARAGRAPHS  # noqa: E402

CALLS = []
DICTIONARY = [("social contract", "contrato social"), ("the", "el"), ("The", "El"), ("and", "y"), ("of", "de")]


def to_spanish(fragment):
    parts = re.split(r"(<[^>]+>)", fragment)
    protected = 0
    stack = []
    output = []
    for part in parts:
        if part.startswith("<"):
            if part.startswith("</"):
                if stack and stack.pop():
                    protected -= 1
            else:
                is_protected = "notranslate" in part
                stack.append(is_protected)
                protected += is_protected
            output.append(part)
        elif protected:
            output.append(part)
        else:
            for source, target in DICTIONARY:
                part = re.sub(rf"\b{source}\b", target, part)
            output.append(part)
    return "".join(output)


class FakeServices(BaseHTTPRequestHandler):
    quota_left = None

    def log_message(self, *args):
        pass

    def _send(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/v2/usage"):
            self._send(200, {"character_count": 100, "character_limit": 500000})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        CALLS.append((self.path, dict(self.headers), body))
        if self.path.startswith("/v2/translate"):
            assert self.headers["Authorization"].startswith("DeepL-Auth-Key ")
            assert body["tag_handling"] == "html" and body["target_lang"] == "ES"
            if FakeServices.quota_left is not None:
                if FakeServices.quota_left <= 0:
                    return self._send(456, {"message": "Quota exceeded"})
                FakeServices.quota_left -= 1
            return self._send(200, {"translations": [{"text": to_spanish(t), "detected_source_language": "EN"} for t in body["text"]]})
        if self.path.startswith("/translate?"):
            assert self.headers["Ocp-Apim-Subscription-Key"] == "azure-key"
            assert self.headers["Ocp-Apim-Subscription-Region"] == "westeurope"
            assert "api-version=3.0" in self.path and "textType=html" in self.path and "to=es" in self.path
            return self._send(200, [{"translations": [{"text": to_spanish(i["Text"]), "to": "es"}]} for i in body])
        if self.path == "/translate":
            assert body["format"] == "html"
            return self._send(200, {"translatedText": to_spanish(body["q"])})
        if self.path == "/v1/messages":
            assert self.headers["x-api-key"] == "claude-key"
            prompt = body["messages"][0]["content"]
            if "Fragments:" in prompt:
                items = json.loads(prompt.split("Fragments:", 1)[1])
                text = json.dumps([to_spanish(i) for i in items], ensure_ascii=False)
            else:
                text = "## Ideas\n\n- El contrato social es una prueba [cap1]."
            return self._send(200, {"content": [{"type": "text", "text": text}]})
        self._send(404, {})


@pytest.fixture(scope="session")
def services():
    server = HTTPServer(("127.0.0.1", 0), FakeServices)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    os.environ["LUMBRE_DEEPL_URL"] = url
    os.environ["LUMBRE_AZURE_URL"] = url
    os.environ["LUMBRE_CLAUDE_URL"] = url
    yield url
    server.shutdown()


@pytest.fixture()
def env(tmp_path, monkeypatch, services):
    from app import store
    monkeypatch.setattr(store, "DATA", tmp_path)
    monkeypatch.setattr(store, "JOBS", tmp_path / "trabajos")
    monkeypatch.setattr(store, "CONFIG_PATH", tmp_path / "ajustes.json")
    FakeServices.quota_left = None
    CALLS.clear()
    return tmp_path


def translator(tmp_path, engine=None, glossary=None):
    from app.engines import DemoEngine
    from app.pipeline import Translator
    from app.store import Memory
    return Translator(engine or DemoEngine(), Memory(tmp_path / "m.sqlite"), "en", "es", glossary or [])


def epub_text(path, name):
    return zipfile.ZipFile(path).read(name).decode("utf-8")


def test_epub_keeps_structure(env):
    from app import epub_tr
    from app.engines import DeepLEngine
    source = make_epub(env / "a.epub")
    output = env / "b.epub"
    tr = translator(env, DeepLEngine("k:fx"), [("social contract", "contrato social")])
    epub_tr.translate(source, output, None, tr, "es")
    original = zipfile.ZipFile(source)
    result = zipfile.ZipFile(output)
    assert result.namelist()[0] == "mimetype" and result.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
    assert set(original.namelist()) == set(result.namelist())
    assert result.read("OEBPS/foto.png") == original.read("OEBPS/foto.png")
    assert result.read("OEBPS/estilo.css") == original.read("OEBPS/estilo.css")
    chapter = result.read("OEBPS/cap1.xhtml").decode()
    assert 'class="titulo"' in chapter and 'id="c1"' in chapter
    assert '<em>contrato social</em>' in chapter
    assert 'epub:type="noteref"' in chapter and 'href="notas.xhtml#n1"' in chapter
    assert 'href="https://example.org/fuente"' in chapter and "<b>original</b>" in chapter
    assert '<img src="foto.png"' in chapter
    assert "code stays exactly as written" in chapter
    assert 'lang="es"' in chapter and 'xml:lang="es"' in chapter
    assert "notranslate" not in chapter
    assert "<dc:language>es</dc:language>" in result.read("OEBPS/libro.opf").decode()
    assert not [i for i in tr.issues if i["tipo"] in ("formato", "vacío", "cifras", "glosario")], tr.issues


def test_epub_bilingual_has_both_languages(env):
    from app import epub_tr
    from app.engines import DeepLEngine
    source = make_epub(env / "a.epub")
    bilingual = env / "bi.epub"
    epub_tr.translate(source, env / "b.epub", bilingual, translator(env, DeepLEngine("k:fx")), "es")
    chapter = epub_text(bilingual, "OEBPS/cap1.xhtml")
    assert "social contract" in chapter and "contrato" not in "" and "lumbre-original" in chapter
    assert chapter.count('id="c1"') == 1 and chapter.count('id="r1"') <= 2


def test_lost_tags_are_reported_and_notes_kept(env):
    from app import epub_tr
    from app.engines import Engine

    class Careless(Engine):
        id = "descuidado"

        def translate(self, fragments, source, target, context=""):
            return [re.sub(r"<[^>]+>", "", f) for f in fragments]

    output = env / "b.epub"
    tr = translator(env, Careless())
    epub_tr.translate(make_epub(env / "a.epub"), output, None, tr, "es")
    chapter = epub_text(output, "OEBPS/cap1.xhtml")
    assert 'href="notas.xhtml#n1"' in chapter
    assert any(i["tipo"] == "formato" for i in tr.issues)


def test_checks_flag_numbers_and_untranslated(env):
    from app.pipeline import Item, check_pair
    item = Item(fragment="In 1762 there were 12 philosophers in the room with him.")
    assert any(i["tipo"] == "cifras" for i in check_pair(item, "En 1763 había 12 filósofos en la sala.", "en", "es", []))
    assert any(i["tipo"] == "sin traducir" for i in check_pair(item, item.text, "en", "es", []))
    assert not check_pair(item, "En 1762 había 12 filósofos en la sala con él.", "en", "es", [])


def test_drm_is_refused(env):
    from app import epub_tr
    path = make_epub(env / "a.epub")
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("META-INF/encryption.xml", '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" xmlns:enc="http://www.w3.org/2001/04/xmlenc#"><enc:EncryptedData><enc:EncryptionMethod Algorithm="http://www.w3.org/2001/04/xmlenc#aes128-cbc"/></enc:EncryptedData></encryption>')
    with pytest.raises(epub_tr.EpubError, match="DRM"):
        epub_tr.analyze(path)


def test_pdf_replaces_text_and_keeps_layout(env):
    from app import pdf_tr
    from app.engines import AzureEngine
    source = make_pdf(env / "a.pdf")
    output = env / "b.pdf"
    bilingual = env / "bi.pdf"
    tr = translator(env, AzureEngine("azure-key", "westeurope"))
    stats = pdf_tr.translate(source, output, bilingual, tr, "es")
    before, after = fitz.open(source), fitz.open(output)
    assert len(after) == len(before) == 3
    text = after[0].get_text()
    assert "contrato social" not in "" and "The social contract" not in text.replace("\n", " ")
    assert "El contrato social" in text.replace("\n", " ")
    assert len(after[0].get_drawings()) >= 1
    assert after.get_toc()[0][2] == 1
    assert len(fitz.open(bilingual)) == 6
    assert stats["no_caben"] == 0


def test_pdf_handles_longer_text_without_overlap(env):
    from app import pdf_tr
    from app.engines import DemoEngine
    source = make_pdf(env / "a.pdf")
    output = env / "b.pdf"
    tr = translator(env, DemoEngine())
    pdf_tr.translate(source, output, None, tr, "es")
    page = fitz.open(output)[1]
    blocks = [fitz.Rect(b[:4]) for b in page.get_text("blocks") if b[6] == 0]
    overlaps = [(a, b) for i, a in enumerate(blocks) for b in blocks[i + 1:] if (a & b).get_area() > 12]
    assert not overlaps


def test_quota_pauses_and_memory_resumes(env):
    from app import epub_tr
    from app.engines import DeepLEngine, EngineError
    from app.store import Memory
    from app.pipeline import Translator
    source = make_epub(env / "a.epub")
    memory = Memory(env / "m.sqlite")
    engine = DeepLEngine("k:fx")
    engine.max_items = 3
    FakeServices.quota_left = 2
    with pytest.raises(EngineError) as failure:
        epub_tr.translate(source, env / "b.epub", None, Translator(engine, memory, "en", "es"), "es")
    assert failure.value.kind == "quota"
    FakeServices.quota_left = None
    resumed = Translator(engine, memory, "en", "es")
    epub_tr.translate(source, env / "b.epub", None, resumed, "es")
    assert resumed.reused > 0 and resumed.sent > 0


def test_all_engines_share_contract(env):
    from app.engines import AzureEngine, ClaudeEngine, DeepLEngine, LibreEngine
    fragment = 'The <span id="t1">social contract</span> <span id="t2" translate="no" class="notranslate">#</span>'
    url = os.environ["LUMBRE_DEEPL_URL"]
    for engine in (DeepLEngine("k:fx"), AzureEngine("azure-key", "westeurope"), LibreEngine(url), ClaudeEngine("claude-key", "claude-sonnet-5-5")):
        result = engine.translate([fragment], "en", "es")[0]
        assert 'id="t1"' in result and "contrato social" in result and result.startswith("El")


def client(env):
    from fastapi.testclient import TestClient
    from app import server
    server.memory = None
    return TestClient(server.app, base_url="http://127.0.0.1:8765")


def wait_done(api, job_id):
    for _ in range(200):
        job = api.get(f"/api/libros/{job_id}").json()
        if job["estado"] not in ("en cola", "traduciendo"):
            return job
        time.sleep(0.05)
    raise AssertionError("no terminó")


def test_full_flow_through_local_api(env):
    api = client(env)
    source = make_epub(env / "ensayo.epub")
    with source.open("rb") as handle:
        job = api.post("/api/libros", files={"file": ("ensayo.epub", handle)}).json()
    assert job["titulo"] == "The Social Contract Today" and job["caracteres"] > 500
    api.post("/api/ajustes", json={"azure_key": "azure-key", "claude_key": "claude-key"})
    settings = api.get("/api/ajustes").json()["ajustes"]
    assert "azure-key" not in json.dumps(settings)
    started = api.post(f"/api/libros/{job['id']}/traducir", json={"motor": "azure", "origen": "en", "destino": "es", "bilingue": True, "glosario": "general will = voluntad general"})
    assert started.status_code == 200
    done = wait_done(api, job["id"])
    assert done["estado"] == "terminado", done
    assert api.get(f"/api/libros/{job['id']}/descargar/traducido").status_code == 200
    assert api.get(f"/api/libros/{job['id']}/descargar/bilingue").status_code == 200
    assert api.get(f"/api/libros/{job['id']}/informe.csv").status_code == 200
    answer = api.post(f"/api/libros/{job['id']}/preguntar", json={"pregunta": "¿Qué dice Rousseau sobre voluntad?"}).json()
    assert answer["pasajes"] and not answer["respuesta"]
    assert api.delete(f"/api/libros/{job['id']}").status_code == 200


def test_requests_from_other_sites_are_blocked(env):
    api = client(env)
    assert api.get("/api/ajustes", headers={"Origin": "https://malicioso.example"}).status_code == 403
    assert api.get("/api/ajustes", headers={"Host": "malicioso.example"}).status_code == 403
    assert api.get("/api/libros/../ajustes").status_code in (200, 404)


def test_invalid_files_get_clear_errors(env):
    api = client(env)
    assert "Solo se admiten" in api.post("/api/libros", files={"file": ("a.txt", b"hola")}).json()["detail"]
    assert api.post("/api/libros", files={"file": ("a.epub", b"no es zip")}).status_code == 400
    assert api.post("/api/libros", files={"file": ("a.pdf", b"no es pdf")}).status_code == 400


def test_pdf_sentence_across_pages_is_translated_once(env):
    from app import pdf_tr
    source = env / "a.pdf"
    document = fitz.open()
    one = document.new_page(width=420, height=595)
    one.insert_textbox(fitz.Rect(50, 300, 370, 400), "The argument that begins on the first page reaches the bottom and the", fontsize=11, fontname="tiro")
    two = document.new_page(width=420, height=595)
    two.insert_textbox(fitz.Rect(50, 100, 370, 200), "sentence continues here on the next page until it ends.", fontsize=11, fontname="tiro")
    document.save(source)
    document.close()
    tr = translator(env)
    seen = []
    original_run = tr.run
    tr.run = lambda items: (seen.extend(items), original_run(items))[1]
    pdf_tr.translate(source, env / "b.pdf", None, tr, "es")
    assert len([i for i in seen if "argument" in i.text]) == 1 and "sentence continues" in seen[0].text
    result = fitz.open(env / "b.pdf")
    assert result[0].get_text().strip() and result[1].get_text().strip()


def test_scanned_page_with_ocr_layer_gets_patched(env):
    from app import pdf_tr
    source = env / "a.pdf"
    document = fitz.open()
    page = document.new_page(width=420, height=595)
    picture = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 210, 298), False)
    picture.set_rect(picture.irect, (240, 232, 215))
    page.insert_image(page.rect, pixmap=picture)
    for index in range(3):
        word = ["The argument that begins here runs along", "several separate lines of recognized text", "and finishes at the end of the paragraph."][index]
        page.insert_text((50, 200 + index * 14), word, fontsize=11, fontname="tiro", render_mode=3)
    document.save(source)
    document.close()
    info = pdf_tr.analyze(source)
    assert any("OCR" in warning for warning in info["avisos"]) and not info.get("bloqueado")
    stages = []
    tr = translator(env)
    tr.on_stage = lambda stage, done, total: stages.append(stage)
    pdf_tr.translate(source, env / "b.pdf", None, tr, "es")
    assert {"maquetando", "guardando"} <= set(stages)
    text = fitz.open(env / "b.pdf")[0].get_text()
    assert "argumenta" in text.replace("\n", " ")
    blocks = [b for b in fitz.open(source)[0].get_text("dict")["blocks"] if b["type"] == 0]
    assert len(pdf_tr._extract(fitz.open(source))[0]) == 1


def test_interrupted_job_is_recovered_on_start(env):
    api = client(env)
    source = make_epub(env / "e.epub")
    with source.open("rb") as handle:
        job = api.post("/api/libros", files={"file": ("e.epub", handle)}).json()
    from app import server
    server.write_job(job["id"], estado="traduciendo", progreso=100)
    server.recover_jobs()
    recovered = api.get(f"/api/libros/{job['id']}").json()
    assert recovered["estado"] == "detenido" and "cuota" in recovered["mensaje"]


def _update_zip(path, version, extra=None):
    root = Path(__file__).parent.parent
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("actualizacion.json", json.dumps({"version": version}))
        for name in ("app/__init__.py", "app/server.py", "app/desktop.py", "app/static/index.html"):
            z.write(root / name, name)
        if extra:
            z.writestr(extra, "x")
    return path


def test_update_installs_and_rejects_bad_files(env):
    from app import updates
    good = _update_zip(env / "u.zip", "9.9.9")
    assert updates.install(good, "0.2.0") == "9.9.9"
    assert (updates.folder() / "9.9.9" / "app" / "server.py").exists()
    assert json.loads((updates.folder() / "actual.json").read_text())["version"] == "9.9.9"
    with pytest.raises(updates.UpdateError, match="Ya tienes"):
        updates.install(_update_zip(env / "v.zip", "0.1.0"), "0.2.0")
    with pytest.raises(updates.UpdateError, match="no permitidas"):
        updates.install(_update_zip(env / "w.zip", "9.9.10", "../evil.py"), "0.2.0")
    (env / "z.zip").write_bytes(b"nada")
    with pytest.raises(updates.UpdateError):
        updates.install(env / "z.zip", "0.2.0")


def test_previous_data_is_recovered(env, monkeypatch):
    from app import migrate, store
    home = env / "casa"
    old = home / "Downloads" / "Lumbre" / "datos"
    (old / "trabajos" / ("a" * 32)).mkdir(parents=True)
    (old / "trabajos" / ("a" * 32) / "estado.json").write_text("{}")
    (old / "ajustes.json").write_text('{"azure_key": "secreta"}')
    (old / "memoria.sqlite").write_text("x")
    monkeypatch.setattr(store, "DATA", env / "nuevo" / "datos")
    monkeypatch.setattr(store, "JOBS", env / "nuevo" / "datos" / "trabajos")
    monkeypatch.setattr(Path, "home", lambda: home)
    migrate.run()
    assert (store.JOBS / ("a" * 32) / "estado.json").exists()
    assert "secreta" in (store.DATA / "ajustes.json").read_text()
    assert migrate.result["libros"] == 1


def test_quality_levels_and_viewer(env):
    from app import calidad
    issues = calidad.check("In 1762 he wrote to the Academy about the general will and consent.", "En 1763 escribió a la Academia sobre la voluntad general y el consentimiento.", "página 3", "en", "es", [])
    assert any(i["tipo"] == "cifras" and i["gravedad"] == "media" for i in issues)
    assert any(i["tipo"] == "lengua original" for i in calidad.check("This is a long source text with many words here.", "the theory of the state and the people of the world and the rights that were", "p", "en", "es", []))
    assert not calidad.check("flTff 2 1 2005 $fiW!E", "flTff 2 1 2005 $fiW!E", "p", "en", "es", [])
    api = client(env)
    pdf = make_pdf(env / "v.pdf")
    with pdf.open("rb") as handle:
        job = api.post("/api/libros", files={"file": ("v.pdf", handle)}).json()
    assert api.get(f"/api/libros/{job['id']}/visor").json()["documentos"]["original"]["paginas"] == 3
    assert api.get(f"/api/libros/{job['id']}/pagina/original/1.jpg").headers["content-type"] == "image/jpeg"
    assert api.get(f"/api/libros/{job['id']}/pagina/original/99.jpg").status_code == 404
    assert api.get(f"/api/libros/{job['id']}/pagina/inventado/1.jpg").status_code == 404
    epub = make_epub(env / "v.epub")
    with epub.open("rb") as handle:
        book = api.post("/api/libros", files={"file": ("v.epub", handle)}).json()
    sections = api.get(f"/api/libros/{book['id']}/visor").json()["documentos"]["original"]["secciones"]
    response = api.get(f"/api/libros/{book['id']}/epub/original/{sections[0]}")
    assert response.status_code == 200 and "sandbox" in response.headers["content-security-policy"]
    assert api.get(f"/api/libros/{book['id']}/epub/original/../../estado.json").status_code == 404


def test_scanned_book_layout_has_no_overlaps(env):
    from app import calidad, pdf_ocr
    source = env / "s.pdf"
    document = fitz.open()
    picture = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 200, 300), False)
    picture.set_rect(picture.irect, (240, 232, 215))
    sentence = "The argument continues along this line of recognized text and"
    for n in range(4):
        page = document.new_page(width=340, height=520)
        page.insert_image(page.rect, pixmap=picture)
        page.insert_text((40, 40), f"{n + 1} THE SOCIAL CONTRACT", fontsize=9, fontname="tiro", render_mode=3)
        for row in range(30):
            page.insert_text((60 if row % 8 == 0 else 40, 70 + row * 13), sentence, fontsize=9.6, fontname="tiro", render_mode=3)
    document.save(source)
    document.close()
    from app.engines import DemoEngine
    opened = fitz.open(source)
    assert pdf_ocr.is_scanned(opened)
    tr = translator(env, DemoEngine())
    pdf_ocr.translate(opened, env / "t.pdf", tr, "es")
    assert not calidad.verify_pdf(env / "t.pdf")
    assert fitz.open(env / "t.pdf")[0].get_images() == []


def _translated_job(api, env, name="q.epub", maker=make_epub, **options):
    source = maker(env / name)
    with source.open("rb") as handle:
        job = api.post("/api/libros", files={"file": (name, handle)}).json()
    body = {"motor": "prueba", "origen": "en", "destino": "es", **options}
    assert api.post(f"/api/libros/{job['id']}/traducir", json=body).status_code == 200
    done = wait_done(api, job["id"])
    assert done["estado"] == "terminado", done
    return done


def test_trash_restore_rename_and_duplicates(env):
    api = client(env)
    job = _translated_job(api, env)
    with (env / "q.epub").open("rb") as handle:
        again = api.post("/api/libros", files={"file": ("copia.epub", handle)}).json()
    assert again["duplicado"] and again["id"] == job["id"]
    assert api.post(f"/api/libros/{job['id']}/datos", json={"titulo": "Otro título", "autor": "A"}).json()["titulo"] == "Otro título"
    assert api.post(f"/api/libros/{job['id']}/datos", json={"titulo": "  "}).status_code == 400
    assert api.delete(f"/api/libros/{job['id']}").status_code == 200
    assert api.get("/api/libros").json() == []
    assert api.get("/api/papelera").json()[0]["titulo"] == "Otro título"
    assert api.post(f"/api/libros/{job['id']}/restaurar").json()["id"] == job["id"]
    assert api.get("/api/libros").json()[0]["id"] == job["id"]
    assert api.post(f"/api/libros/{job['id']}/restaurar").status_code == 404
    api.delete(f"/api/libros/{job['id']}")
    assert api.delete("/api/papelera").status_code == 200 and api.get("/api/papelera").json() == []


def test_correct_replace_and_rebuild_without_spending(env):
    api = client(env)
    job = _translated_job(api, env)
    data = api.get(f"/api/libros/{job['id']}/segmentos", params={"q": "Figure 1"}).json()
    assert data["total"] == 1
    segment = data["segmentos"][0]
    saved = api.put(f"/api/libros/{job['id']}/segmentos/{segment['id']}", json={"traduccion": "Figura uno corregida. La voluntad general."})
    assert saved.status_code == 200
    with_marks = next(s for s in api.get(f"/api/libros/{job['id']}/segmentos", params={"limite": 100}).json()["segmentos"] if "⟦" in s["traduccion"])
    assert api.put(f"/api/libros/{job['id']}/segmentos/{with_marks['id']}", json={"traduccion": "Sin marcas"}).status_code == 400
    assert api.post(f"/api/libros/{job['id']}/reemplazar", json={"buscar": "voluntad general", "simular": True}).json()["coincidencias"] >= 1
    assert api.post(f"/api/libros/{job['id']}/reemplazar", json={"buscar": "voluntad general", "cambiar": "voluntad común"}).json()["parrafos"] >= 1
    assert api.get(f"/api/libros/{job['id']}").json()["cambios_pendientes"]
    assert api.post(f"/api/libros/{job['id']}/segmentos/{segment['id']}/retraducir").status_code == 200
    api.put(f"/api/libros/{job['id']}/segmentos/{segment['id']}", json={"traduccion": "Figura uno & corregida."})
    options = api.get(f"/api/libros/{job['id']}").json()["opciones"]
    api.post(f"/api/libros/{job['id']}/traducir", json={**options, "reconstruir": True})
    rebuilt = wait_done(api, job["id"])
    assert rebuilt["enviados"] == 0 and not rebuilt["cambios_pendientes"]
    assert "Figura uno &amp; corregida." in zipfile.ZipFile(store_file(env, job["id"], "traducido.epub")).read("OEBPS/cap1.xhtml").decode()


def store_file(env, job_id, name):
    return env / "trabajos" / job_id / name


def test_partial_translation_then_complete(env):
    api = client(env)
    job = _translated_job(api, env, "p.pdf", make_pdf, paginas="2")
    assert job["parcial"] == "2" and job["enviados"] > 0
    first = fitz.open(store_file(env, job["id"], "traducido.pdf"))
    assert "Questiona" not in first[0].get_text() and "Questiona" in first[1].get_text()
    bad = api.post(f"/api/libros/{job['id']}/traducir", json={"motor": "prueba", "origen": "en", "destino": "es", "paginas": "x"})
    assert bad.status_code == 400
    options = {k: v for k, v in job["opciones"].items() if k != "paginas"}
    api.post(f"/api/libros/{job['id']}/traducir", json=options)
    complete = wait_done(api, job["id"])
    assert complete["parcial"] == "" and complete["reutilizados"] > 0
    assert "Questiona" in fitz.open(store_file(env, job["id"], "traducido.pdf"))[0].get_text()


def test_fresh_retranslation_ignores_memory_and_general_glossary(env):
    api = client(env)
    job = _translated_job(api, env)
    assert job["enviados"] > 0
    options = job["opciones"]
    api.post(f"/api/libros/{job['id']}/traducir", json=options)
    assert wait_done(api, job["id"])["enviados"] == 0
    api.post(f"/api/libros/{job['id']}/traducir", json={**options, "sin_memoria": True})
    assert wait_done(api, job["id"])["enviados"] > 0
    api.post("/api/ajustes", json={"glosario_general": "general will = voluntad general"})
    api.post(f"/api/libros/{job['id']}/traducir", json=options)
    done = wait_done(api, job["id"])
    assert "general will = voluntad general" in done["opciones"]["glosario"] and done["opciones"]["glosario_libro"] == ""
    assert "voluntad general" in zipfile.ZipFile(store_file(env, job["id"], "traducido.epub")).read("OEBPS/cap1.xhtml").decode()


def test_export_viewer_text_and_storage(env):
    api = client(env)
    job = _translated_job(api, env)
    text = api.get(f"/api/libros/{job['id']}/exportar/txt")
    assert text.status_code == 200 and "attachment" in text.headers["content-disposition"] and len(text.text) > 200
    epub = api.get(f"/api/libros/{job['id']}/exportar/epub")
    target = env / "e.epub"
    target.write_bytes(epub.content)
    names = zipfile.ZipFile(target).namelist()
    assert names[0] == "mimetype" and "OEBPS/nav.xhtml" in names
    from app import epub_tr
    assert epub_tr.analyze(target)["caracteres"] > 200
    assert api.get(f"/api/libros/{job['id']}/exportar/doc").status_code == 404
    assert len(api.get(f"/api/libros/{job['id']}/texto/traducido/1").json()["texto"]) > 20
    assert api.get("/api/almacenamiento").json()["libros"] > 0


class StudyAI(BaseHTTPRequestHandler):
    calls = []
    rate_limited = False

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        prompt = body["messages"][0]["content"]
        StudyAI.calls.append(prompt)
        if StudyAI.rate_limited:
            StudyAI.rate_limited = False
            self.send_response(429)
            self.send_header("Retry-After", "0")
            self.end_headers()
            return
        if "NOTAS:" in prompt:
            reply = {"mega_resumen": "Resumen global.", "ideas_clave": ["Idea uno"], "temas": [{"titulo": "Tema global", "descripcion": "Dos frases.", "bloques": [0]}]}
        elif "TEXTO:" in prompt:
            place = re.search(r"\n\[([^\]]+)\]", prompt.split("TEXTO:")[1]).group(1)
            reply = {
                "tema": "Tema", "resumen": "Resumen del bloque.",
                "puntos": [{"texto": "Punto real", "lugar": place}, {"texto": "Punto con lugar inventado", "lugar": "página 9999"}],
                "conceptos": [{"termino": "Voluntad", "definicion": "Definición", "lugar": place}],
                "cronologia": [], "personas": [],
                "test": [
                    {"pregunta": "Buena", "opciones": ["a", "b", "c", "d"], "correcta": 1, "explicacion": "x", "lugar": place},
                    {"pregunta": "Rota", "opciones": ["a", "b"], "correcta": 0},
                    {"pregunta": "Fuera de rango", "opciones": ["a", "b", "c", "d"], "correcta": 7},
                    {"pregunta": "Repetidas", "opciones": ["a", "a", "c", "d"], "correcta": 0},
                ],
                "desarrollo": [{"pregunta": "Explica", "respuesta": "Respuesta", "lugar": place}],
                "tarjetas": [{"frente": "<img src=x onerror=alert(1)>", "dorso": "Dorso", "lugar": place}],
            }
        else:
            reply = None
        content = json.dumps(reply) if reply else "funciona"
        data = json.dumps({"choices": [{"message": {"content": "```json\n" + content + "\n```" if reply else content}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def wait_study(api, job_id):
    for _ in range(200):
        state = api.get(f"/api/libros/{job_id}/estudio").json()
        if state["trabajo"]["estado"] != "preparando":
            return state
        time.sleep(0.05)
    raise AssertionError("El estudio no termina")


def test_study_material_with_and_without_ai(env):
    server = HTTPServer(("127.0.0.1", 0), StudyAI)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    api = client(env)
    path = make_epub(env / "estudio.epub")
    with path.open("rb") as handle:
        job = api.post("/api/libros", files={"file": ("estudio.epub", handle)}).json()
    assert api.post(f"/api/libros/{job['id']}/traducir", json={"motor": "prueba", "origen": "en", "destino": "es"}).status_code == 200
    assert wait_done(api, job["id"])["estado"] == "terminado"

    assert api.post("/api/ia/probar", json={}).json()["ok"] is False
    assert api.post(f"/api/libros/{job['id']}/estudio", json={"metodo": "local"}).status_code == 200
    local = wait_study(api, job["id"])
    assert local["trabajo"]["estado"] == "listo" and local["material"]["temas"] and "aviso" in local["material"]

    api.post("/api/ajustes", json={"ia_motor": "personalizada", "ia_personalizada_clave": "clave-secreta-larga", "ia_personalizada_url": f"http://127.0.0.1:{server.server_port}/v1", "ia_personalizada_modelo": "m"})
    assert "clave-secreta-larga" not in api.get("/api/ajustes").text
    assert api.post("/api/ia/probar", json={}).json()["ok"] is True

    StudyAI.calls.clear()
    StudyAI.rate_limited = True
    assert api.post(f"/api/libros/{job['id']}/estudio", json={"metodo": "ia", "modo": "expres"}).status_code == 200
    state = wait_study(api, job["id"])
    assert state["trabajo"]["estado"] == "listo", state["trabajo"]
    pack = state["material"]
    assert pack["mega_resumen"] == "Resumen global." and pack["temas"][0]["titulo"] == "Tema global"
    assert [q["pregunta"] for q in pack["test"]] == ["Buena"]
    invented = [p for p in pack["puntos"] if p["texto"].startswith("Punto con")]
    assert invented and invented[0]["lugar"] == ""
    first_calls = len(StudyAI.calls)

    assert api.post(f"/api/libros/{job['id']}/estudio", json={"metodo": "ia", "modo": "expres"}).status_code == 200
    assert wait_study(api, job["id"])["trabajo"]["estado"] == "listo"
    assert len(StudyAI.calls) == first_calls, "Lo ya hecho no debe volver a gastar cuota"

    assert api.put(f"/api/libros/{job['id']}/estudio/progreso", json={"tarjetas": {"a": {"c": 2}}}).status_code == 200
    assert api.get(f"/api/libros/{job['id']}/estudio").json()["progreso"]["tarjetas"]["a"]["c"] == 2
    exported = api.get(f"/api/libros/{job['id']}/estudio/exportar/md")
    assert exported.status_code == 200 and "Resumen global." in exported.text
    anki = api.get(f"/api/libros/{job['id']}/estudio/exportar/anki")
    assert "&lt;img" in anki.text and "<img" not in anki.text
    answer = api.post(f"/api/libros/{job['id']}/preguntar", json={"pregunta": PARAGRAPHS[0][:80], "metodo": "ia"}).json()
    assert answer["respuesta"] == "funciona"
    server.shutdown()
