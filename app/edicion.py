"""Corrección de la traducción y exportación, sin volver a gastar cuota."""
from __future__ import annotations

import html
import json
import re
import threading
import zipfile
from pathlib import Path

_lock = threading.Lock()
ATOMIC = re.compile(r'<span\b[^>]*\bid="t(\d+)"[^>]*\bnotranslate\b[^>]*>.*?</span>|<span\b[^>]*\bnotranslate\b[^>]*\bid="t(\d+)"[^>]*>.*?</span>', re.S)
OPEN = re.compile(r'<span\b[^>]*\bid="t(\d+)"[^>]*>')


def to_editable(fragment: str) -> str:
    """Muestra las marcas internas como ⟦n⟧ para que no estorben ni se pierdan."""
    text = ATOMIC.sub(lambda m: f"⟦{m.group(1) or m.group(2)}⟧", fragment)
    text = re.sub(r'<span\b[^>]*\bnotranslate\b[^>]*>(.*?)</span>', r"\1", text, flags=re.S)
    text = OPEN.sub(lambda m: f"⟦{m.group(1)}:", text)
    text = text.replace("</span>", "⟧")
    return html.unescape(text)


def from_editable(text: str) -> str:
    keep: list[str] = []

    def stash(match: re.Match) -> str:
        keep.append(match.group(0))
        return f"\x00{len(keep) - 1}\x00"

    text = re.sub(r"</?[ib]>", stash, text)
    text = html.escape(text, quote=False)
    text = re.sub(r"⟦(\d+)⟧", r'<span id="t\1" translate="no" class="notranslate">#</span>', text)
    text = re.sub(r"⟦(\d+):", r'<span id="t\1">', text).replace("⟧", "</span>")
    return re.sub(r"\x00(\d+)\x00", lambda m: keep[int(m.group(1))], text)


def valid_marks(original: str, edited: str) -> bool:
    marks = lambda value: sorted(re.findall(r"⟦\d+[⟧:]", value))
    return marks(original) == marks(edited) and edited.count("⟧") == original.count("⟧")


def load(directory: Path) -> list[dict]:
    path = directory / "segmentos.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def save(directory: Path, segments: list[dict]) -> None:
    with _lock:
        temporary = directory / "segmentos.tmp"
        temporary.write_text(json.dumps(segments, ensure_ascii=False), encoding="utf-8")
        temporary.replace(directory / "segmentos.json")


def page_of(where: str) -> int | None:
    match = re.search(r"página (\d+)", where)
    return int(match.group(1)) if match else None


def parse_range(text: str, limit: int | None = None) -> list[int]:
    """Convierte «40-45, 50» en una lista de páginas. Lanza ValueError si no se entiende."""
    pages: set[int] = set()
    for part in re.split(r"[,;\s]+", (text or "").strip()):
        if not part:
            continue
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not match:
            raise ValueError(part)
        first = int(match.group(1))
        last = int(match.group(2) or first)
        if first < 1 or last < first or last - first > 100_000:
            raise ValueError(part)
        pages.update(range(first, last + 1))
    if limit:
        pages = {page for page in pages if page <= limit}
    return sorted(pages)


def _chapters(segments: list[dict]) -> list[tuple[str, list[dict]]]:
    chapters: list[tuple[str, list[dict]]] = []
    for segment in segments:
        if segment["lugar"] in ("índice", "metadatos") or segment.get("tipo") in ("cabecera",):
            continue
        if segment.get("tipo") == "titulo" or not chapters:
            title = plain_text(segment["traduccion"]) if segment.get("tipo") == "titulo" else "Inicio"
            chapters.append((title, []))
            if segment.get("tipo") == "titulo":
                continue
        chapters[-1][1].append(segment)
    return chapters


def plain_text(fragment: str) -> str:
    fragment = re.sub(r'<span\b[^>]*\bnotranslate\b[^>]*>#</span>', "", fragment)
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def export_txt(segments: list[dict], title: str) -> str:
    lines = [title, ""]
    for heading, body in _chapters(segments):
        lines += ["", heading.upper(), ""]
        lines += [plain_text(segment["traduccion"]) + "\n" for segment in body]
    return "\n".join(lines)


def export_epub(segments: list[dict], destination: Path, title: str, author: str, language: str, identifier: str) -> None:
    chapters = _chapters(segments)
    esc = lambda value: html.escape(value, quote=True)
    with zipfile.ZipFile(destination, "w") as out:
        out.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        out.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/libro.opf" media-type="application/oebps-package+xml"/></rootfiles></container>', compress_type=zipfile.ZIP_DEFLATED)
        out.writestr("OEBPS/estilo.css", "body{font-family:serif;line-height:1.5}h1{font-size:1.4em;text-align:center;margin:2em 0 1em}p{margin:0;text-indent:1.2em;text-align:justify}.nota{font-size:.85em;text-indent:0;margin-top:.6em}", compress_type=zipfile.ZIP_DEFLATED)
        items, spine, nav = [], [], []
        for number, (heading, body) in enumerate(chapters, 1):
            name = f"cap{number}.xhtml"
            paragraphs = "".join(
                f'<p{" class=\"nota\"" if segment.get("tipo") == "nota" else ""}>{esc(plain_text(segment["traduccion"]))}</p>' for segment in body
            )
            page = f'<?xml version="1.0" encoding="utf-8"?><!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml" lang="{esc(language)}" xml:lang="{esc(language)}"><head><title>{esc(heading)}</title><link rel="stylesheet" href="estilo.css" type="text/css"/></head><body><section><h1>{esc(heading)}</h1>{paragraphs}</section></body></html>'
            out.writestr(f"OEBPS/{name}", page, compress_type=zipfile.ZIP_DEFLATED)
            items.append(f'<item id="c{number}" href="{name}" media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="c{number}"/>')
            nav.append(f'<li><a href="{name}">{esc(heading)}</a></li>')
        out.writestr("OEBPS/nav.xhtml", f'<?xml version="1.0" encoding="utf-8"?><!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="{esc(language)}" xml:lang="{esc(language)}"><head><title>Índice</title></head><body><nav epub:type="toc"><h1>Índice</h1><ol>{"".join(nav)}</ol></nav></body></html>', compress_type=zipfile.ZIP_DEFLATED)
        creator = f"<dc:creator>{esc(author)}</dc:creator>" if author else ""
        out.writestr("OEBPS/libro.opf", f'<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="uid">urn:lumbre:{esc(identifier)}</dc:identifier><dc:title>{esc(title)}</dc:title>{creator}<dc:language>{esc(language)}</dc:language><meta property="dcterms:modified">2026-01-01T00:00:00Z</meta></metadata><manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/><item id="css" href="estilo.css" media-type="text/css"/>{"".join(items)}</manifest><spine>{"".join(spine)}</spine></package>', compress_type=zipfile.ZIP_DEFLATED)
