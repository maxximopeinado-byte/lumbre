"""Traduce un PDF nativo sustituyendo cada párrafo en su sitio."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf as fitz

from . import pdf_ocr, tipos
from .pipeline import Cancelled, Item, Translator, has_letters, normalize, plain

TERMINAL = tuple('.!?:;»"”’)')
MIN_SCALE_STEPS = (0.85, 0.7, 0.55)


class PdfError(Exception):
    pass


@dataclass
class Paragraph:
    page: int
    lines: list = field(default_factory=list)
    fragment: str = ""
    size: float = 11.0
    color: int = 0
    serif: bool = True
    mono: bool = False
    bold: bool = False
    italic: bool = False
    center: bool = False
    leading: float = 1.2
    item: Item | None = None
    continues: "Paragraph | None" = None
    image_backed: bool = False

    @property
    def rect(self) -> fitz.Rect:
        rect = fitz.Rect(self.lines[0]["bbox"])
        for line in self.lines[1:]:
            rect |= fitz.Rect(line["bbox"])
        return rect


def open_pdf(path: Path) -> fitz.Document:
    try:
        document = fitz.open(path)
    except (fitz.FileDataError, RuntimeError) as exc:
        raise PdfError("El archivo no es un PDF válido.") from exc
    if document.needs_pass or document.is_encrypted:
        document.close()
        raise PdfError("Este PDF tiene contraseña o protección y no se puede traducir.")
    return document


def _style(line_spans: list[dict]) -> dict:
    weights: dict[tuple, int] = {}
    for span in line_spans:
        key = (round(span["size"], 1), span["color"], span["flags"] & 2 > 0, span["flags"] & 16 > 0 or "bold" in span["font"].lower(), span["flags"] & 4 > 0, span["flags"] & 8 > 0)
        weights[key] = weights.get(key, 0) + len(span["text"])
    size, color, italic, bold, serif, mono = max(weights, key=weights.get)
    return {"size": size, "color": color, "italic": italic, "bold": bold, "serif": serif, "mono": mono}


def _split_paragraphs(block: dict, page: int) -> list[Paragraph]:
    lines = [
        line for line in block["lines"]
        if abs(line["dir"][0] - 1) < 0.01 and "".join(s["text"] for s in line["spans"]).strip()
    ]
    if not lines:
        return []
    left = min(line["bbox"][0] for line in lines)
    right = max(line["bbox"][2] for line in lines)
    groups: list[list[dict]] = [[lines[0]]]
    for previous, line in zip(lines, lines[1:]):
        height = max(1.0, previous["bbox"][3] - previous["bbox"][1])
        gap = line["bbox"][1] - previous["bbox"][3]
        indent = line["bbox"][0] - left
        previous_text = "".join(s["text"] for s in previous["spans"]).rstrip()
        short = previous["bbox"][2] < left + (right - left) * 0.88
        new_para = (
            gap > height * 0.7
            or (indent > height * 0.8 and previous["bbox"][0] - left < height * 0.4 and previous_text.endswith(TERMINAL))
            or (short and previous_text.endswith((".", "!", "?", "»", '"', "”")))
        )
        if new_para:
            groups.append([line])
        else:
            groups[-1].append(line)
    return [_build(group, page) for group in groups]


def _build(lines: list[dict], page: int) -> Paragraph:
    spans = [span for line in lines for span in line["spans"] if span["text"].strip()]
    style = _style(spans) if spans else {"size": 11, "color": 0, "italic": False, "bold": False, "serif": True, "mono": False}
    pieces: list[str] = []
    for index, line in enumerate(lines):
        line_text = []
        for span in line["spans"]:
            text = html.escape(span["text"], quote=False)
            if not span["text"].strip():
                line_text.append(text)
                continue
            is_italic = span["flags"] & 2 > 0
            is_bold = span["flags"] & 16 > 0 or "bold" in span["font"].lower()
            if is_italic and not style["italic"]:
                text = f"<i>{text}</i>"
            if is_bold and not style["bold"]:
                text = f"<b>{text}</b>"
            line_text.append(text)
        joined = "".join(line_text).rstrip()
        if index < len(lines) - 1 and re.search(r"[^\W\d_]-(</[ib]>)*$", joined):
            joined = re.sub(r"-((?:</[ib]>)*)$", r"\1", joined)
            pieces.append(joined)
        else:
            pieces.append(joined + " ")
    fragment = re.sub(r"\s+", " ", "".join(pieces)).strip()
    fragment = re.sub(r"</i>(\s*)<i>", r"\1", fragment)
    fragment = re.sub(r"</b>(\s*)<b>", r"\1", fragment)
    paragraph = Paragraph(page=page, lines=lines, fragment=normalize(fragment), **style)
    if len(lines) >= 2:
        pitches = [b["bbox"][1] - a["bbox"][1] for a, b in zip(lines, lines[1:])]
        pitches = [p for p in pitches if p > 0]
        if pitches:
            paragraph.leading = max(1.0, min(1.8, sorted(pitches)[len(pitches) // 2] / max(1.0, paragraph.size)))
        rect = paragraph.rect
        centers = [(line["bbox"][0] + line["bbox"][2]) / 2 for line in lines]
        lefts = [line["bbox"][0] for line in lines]
        paragraph.center = max(centers) - min(centers) < 3 and max(lefts) - min(lefts) > 5
    else:
        rect = paragraph.rect
    return paragraph


def _image_backed(page: fitz.Page) -> bool:
    """Una imagen ocupa la página: suele ser un escaneado con capa de texto (OCR)."""
    area = abs(page.rect) or 1
    largest = 0.0
    for info in page.get_image_info():
        largest = max(largest, abs(fitz.Rect(info["bbox"]) & page.rect))
    return largest >= area * 0.8


def _merge_lines(found: list[Paragraph], number: int) -> list[Paragraph]:
    """El OCR suele separar cada línea en un bloque: las reúne en párrafos."""
    merged: list[Paragraph] = []
    for paragraph in found:
        previous = merged[-1] if merged else None
        if previous is not None:
            a, b = previous.rect, paragraph.rect
            height = max(1.0, previous.lines[-1]["bbox"][3] - previous.lines[-1]["bbox"][1])
            same_column = min(a.x1, b.x1) - max(a.x0, b.x0) > 0.5 * min(a.width, b.width)
            close = -height * 0.3 <= b.y0 - a.y1 < height * 0.7
            same_size = abs(previous.size - paragraph.size) <= max(0.8, previous.size * 0.12)
            text = plain(previous.fragment)
            following = plain(paragraph.fragment)
            unfinished = not text.endswith(TERMINAL) or following[:1].islower()
            if same_column and close and same_size and unfinished:
                merged[-1] = _build(previous.lines + paragraph.lines, number)
                continue
        merged.append(paragraph)
    return merged


def _extract(document: fitz.Document) -> list[list[Paragraph]]:
    pages: list[list[Paragraph]] = []
    for number, page in enumerate(document):
        found: list[Paragraph] = []
        backed = _image_backed(page)
        data = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE | fitz.TEXT_MEDIABOX_CLIP)
        for block in data["blocks"]:
            if block.get("type") != 0:
                continue
            for paragraph in _split_paragraphs(block, number):
                if paragraph.size >= 4:
                    found.append(paragraph)
        found = _merge_lines(found, number)
        for paragraph in found:
            paragraph.image_backed = backed
        pages.append(found)
    return pages


def _is_margin(paragraph: Paragraph, page_height: float) -> bool:
    rect = paragraph.rect
    return (rect.y1 < page_height * 0.09 or rect.y0 > page_height * 0.91) and len(paragraph.fragment) < 90


def _link_across_pages(document: fitz.Document, pages: list[list[Paragraph]]) -> None:
    for number in range(len(pages) - 1):
        height = document[number].rect.height
        body = [p for p in pages[number] if not _is_margin(p, height) and has_letters(plain(p.fragment))]
        next_body = [p for p in pages[number + 1] if not _is_margin(p, document[number + 1].rect.height) and has_letters(plain(p.fragment))]
        if not body or not next_body:
            continue
        last = max(body, key=lambda p: (p.rect.y1, p.rect.x0))
        first = min(next_body, key=lambda p: (p.rect.y0, p.rect.x0))
        text = plain(last.fragment)
        following = plain(first.fragment)
        if not text.endswith(TERMINAL) and following[:1].islower() and abs(last.size - first.size) < 0.6:
            last.continues = first


def analyze(path: Path) -> dict:
    document = open_pdf(path)
    try:
        return _analyze(document)
    finally:
        document.close()


def _analyze(document: fitz.Document) -> dict:
    pages = _extract(document)
    without_text = sum(1 for page in pages if not page)
    samples = [
        {"lugar": f"página {index + 1}", "texto": plain(p.fragment)}
        for index, page in enumerate(pages) for p in page if has_letters(plain(p.fragment))
    ]
    characters = sum(len(s["texto"]) for s in samples)
    warnings = []
    if len(pages) and without_text / len(pages) > 0.5:
        warnings.append("Parece un PDF escaneado. Esta versión necesita texto seleccionable y todavía no incluye OCR.")
    elif without_text:
        warnings.append(f"{without_text} páginas no tienen texto seleccionable y quedarán como están.")
    meta = document.metadata or {}
    info = {
        "tipo": "pdf", "titulo": meta.get("title") or "", "autor": meta.get("author") or "", "idioma": "",
        "unidades": len(pages), "unidad": "páginas", "caracteres": characters,
        "avisos": warnings, "parrafos": samples,
        "bloqueado": len(pages) > 0 and without_text / len(pages) > 0.5,
    }
    if not info["bloqueado"] and pdf_ocr.is_scanned(document):
        rebuilt = pdf_ocr.analyze(document)
        info["parrafos"] = rebuilt["parrafos"]
        info["caracteres"] = sum(len(sample["texto"]) for sample in rebuilt["parrafos"])
        info["avisos"] = rebuilt["avisos"]
        info["escaneado"] = True
    return info


def _split_for_pages(text: str, first_share: float) -> tuple[str, str]:
    words = text.split(" ")
    target = len(text) * first_share
    used = 0
    for index, word in enumerate(words):
        used += len(word) + 1
        if used >= target:
            return " ".join(words[: index + 1]), " ".join(words[index + 1:])
    return text, ""


def _obstacles(paragraph: Paragraph, others: list[Paragraph], page_rect: fitz.Rect) -> float:
    rect = paragraph.rect
    limit = page_rect.y1 - min(36, page_rect.height * 0.05)
    for other in others:
        if other is paragraph:
            continue
        other_rect = other.rect
        overlaps = other_rect.x0 < rect.x1 and other_rect.x1 > rect.x0
        if overlaps and other_rect.y0 >= rect.y1 - 1:
            limit = min(limit, other_rect.y0 - 1)
    return max(limit, rect.y1)


def _paper_color(pixmap: fitz.Pixmap, rect: fitz.Rect, page_rect: fitz.Rect) -> tuple[float, float, float]:
    """Toma el color del papel alrededor del párrafo para taparlo sin que se note."""
    sx = pixmap.width / page_rect.width
    sy = pixmap.height / page_rect.height
    samples = []
    for step in range(9):
        x = rect.x0 + rect.width * step / 8
        for y in (rect.y0 - 2, rect.y1 + 2):
            px = min(pixmap.width - 1, max(0, int((x - page_rect.x0) * sx)))
            py = min(pixmap.height - 1, max(0, int((y - page_rect.y0) * sy)))
            samples.append(pixmap.pixel(px, py)[:3])
    channels = [sorted(sample[i] for sample in samples)[len(samples) // 2] / 255 for i in range(3)]
    return tuple(channels)


def _css(paragraph: Paragraph, multi_line: bool) -> str:
    family = "monospace" if paragraph.mono else ("serif" if paragraph.serif or paragraph.image_backed else "sans-serif")
    color = "#262320" if paragraph.image_backed else f"#{paragraph.color & 0xFFFFFF:06x}"
    align = "center" if paragraph.center else ("justify" if multi_line else "left")
    weight = "bold" if paragraph.bold else "normal"
    style = "italic" if paragraph.italic else "normal"
    return (
        f"* {{margin:0;padding:0}} body {{font-family:{family};font-size:{paragraph.size}pt;color:{color};"
        f"line-height:{paragraph.leading:.2f};text-align:{align};font-weight:{weight};font-style:{style}}}"
    )


_FONTS = {
    "serif": {(False, False): "tiro", (True, False): "tibo", (False, True): "tiit", (True, True): "tibi"},
    "sans": {(False, False): "helv", (True, False): "hebo", (False, True): "heit", (True, True): "hebi"},
    "mono": {(False, False): "cour", (True, False): "cobo", (False, True): "coit", (True, True): "cobi"},
}


def _words(content: str, paragraph: Paragraph) -> list[tuple[str, bool, bool]] | None:
    """Separa el párrafo en palabras con su estilo. None si necesita otra fuente."""
    bold, italic = paragraph.bold, paragraph.italic
    words: list[tuple[str, bool, bool]] = []
    for part in re.split(r"(</?[ib]\b[^>]*>|</?[^>]+>)", content):
        lower = part.lower()
        if lower.startswith("<b"):
            bold = True
        elif lower.startswith("</b"):
            bold = paragraph.bold
        elif lower.startswith("<i"):
            italic = True
        elif lower.startswith("</i"):
            italic = paragraph.italic
        elif not part.startswith("<"):
            text = html.unescape(part)
            pieces = re.split(r"\s+", text)
            for index, piece in enumerate(pieces):
                if not piece:
                    continue
                glued = index == 0 and words and not text[:1].isspace()
                if glued:
                    previous = words[-1]
                    words[-1] = (previous[0], previous[1], previous[2])
                    words.append(("\x00" + piece, bold, italic))
                else:
                    words.append((piece, bold, italic))
    if not tipos.has_glyphs("".join(word for word, _, _ in words).replace("\x00", "")):
        return None
    return words


def _wrap(words, family: dict, size: float, width: float) -> list[list[tuple[str, str, float, bool]]]:
    lines: list[list[tuple[str, str, float, bool]]] = [[]]
    used = 0.0
    space = tipos.width(" ", family[(False, False)], size)
    for word, bold, italic in words:
        glued = word.startswith("\x00")
        clean = word.lstrip("\x00")
        font = family[(bold, italic)]
        length = tipos.width(clean, font, size)
        extra = 0 if glued or not lines[-1] else space
        if lines[-1] and used + extra + length > width and not glued:
            lines.append([])
            used = 0.0
            extra = 0
        lines[-1].append((clean, font, length, glued))
        used += extra + length
    return lines


def _draw(shape, lines, rect: fitz.Rect, paragraph: Paragraph, size: float, leading: float, color, justify: bool) -> None:
    y = rect.y0 + size * 0.95
    for index, line in enumerate(lines):
        space = tipos.width(" ", line[0][1], size) if line else 0.0
        natural = sum(length for _, _, length, _ in line)
        gaps = sum(1 for position, entry in enumerate(line) if position and not entry[3])
        gap = space
        x = rect.x0
        if paragraph.center:
            x = rect.x0 + max(0.0, (rect.width - natural - gaps * space) / 2)
        elif justify and gaps and index < len(lines) - 1:
            gap = min(space * 2.6, (rect.width - natural) / gaps)
        for position, (word, font, length, glued) in enumerate(line):
            if position and not glued:
                x += gap
            tipos.draw(shape, x, y, word, font, size, color)
            x += length
        y += size * leading


def _place(page: fitz.Page, shape, paragraph: Paragraph, content: str, others: list[Paragraph]) -> tuple[bool, float]:
    """Compone el párrafo con fuentes estándar, que no aumentan el peso del PDF."""
    rect = paragraph.rect
    box = fitz.Rect(rect.x0, rect.y0, rect.x1 + 1, rect.y1 + 1)
    grown = fitz.Rect(box.x0, box.y0, box.x1, max(box.y1, _obstacles(paragraph, others, page.rect)))
    if len(paragraph.lines) == 1 and not paragraph.center:
        grown.x1 = max(grown.x1, min(page.rect.x1 - 30, grown.x0 + (grown.x1 - grown.x0) * 1.25))
    words = _words(content, paragraph)
    if words is None:
        css = _css(paragraph, len(paragraph.lines) > 1)
        for candidate, low in ((box, 1), (grown, MIN_SCALE_STEPS[0]), (grown, MIN_SCALE_STEPS[2])):
            spare, scale = page.insert_htmlbox(candidate, content, css=css, scale_low=low)
            if spare >= 0:
                return True, scale
        _, scale = page.insert_htmlbox(grown, content, css=css, scale_low=0)
        return False, scale
    if not words:
        return True, 1.0
    style = "mono" if paragraph.mono else ("serif" if paragraph.serif or paragraph.image_backed else "sans")
    family = _FONTS[style]
    tipos.register(page, set(family.values()))
    value = paragraph.color & 0xFFFFFF
    color = (0.15, 0.14, 0.125) if paragraph.image_backed else ((value >> 16) / 255, ((value >> 8) & 255) / 255, (value & 255) / 255)
    justify = len(paragraph.lines) > 1
    attempts = [(box, 1.0), (grown, 1.0), (grown, 0.93), (grown, 0.85), (grown, 0.77), (grown, 0.7), (grown, 0.62), (grown, 0.55)]
    final = None
    for candidate, scale in attempts:
        size = paragraph.size * scale
        lines = _wrap(words, family, size, candidate.width)
        final = (candidate, scale, lines, size)
        if size * 1.05 + (len(lines) - 1) * size * paragraph.leading <= candidate.height + size * 0.2:
            _draw(shape, lines, candidate, paragraph, size, paragraph.leading, color, justify)
            return True, scale
    candidate, scale, lines, size = final
    tight = max(0.95, min(paragraph.leading, (candidate.height - size) / max(1, len(lines) - 1) / size if len(lines) > 1 else paragraph.leading))
    _draw(shape, lines, candidate, paragraph, size, tight, color, justify)
    return False, scale


def translate(path: Path, output: Path, bilingual_output: Path | None, translator: Translator, target: str) -> dict:
    document = open_pdf(path)
    try:
        return _translate(document, path, output, bilingual_output, translator, target)
    finally:
        document.close()


def _translate(document: fitz.Document, path: Path, output: Path, bilingual_output: Path | None, translator: Translator, target: str) -> dict:
    if pdf_ocr.is_scanned(document):
        stats = pdf_ocr.translate(document, output, translator, target)
        _make_bilingual(path, output, bilingual_output)
        return stats
    pages = _extract(document)
    _link_across_pages(document, pages)

    continued = {id(p.continues) for page in pages for p in page if p.continues is not None}
    items: list[Item] = []
    ordered: list[Paragraph] = []
    for index, page in enumerate(pages):
        for paragraph in page:
            text = plain(paragraph.fragment)
            if not has_letters(text) or id(paragraph) in continued:
                continue
            fragment = paragraph.fragment
            where = f"página {index + 1}"
            if paragraph.continues is not None:
                # Se traduce la frase entera y luego se reparte entre las dos páginas.
                fragment = f"{plain(fragment).replace('&', '&amp;').replace('<', '&lt;')} {plain(paragraph.continues.fragment).replace('&', '&amp;').replace('<', '&lt;')}"
            paragraph.item = Item(fragment=fragment, where=where)
            items.append(paragraph.item)
            ordered.append(paragraph)

    toc = document.get_toc(simple=False)
    toc_items = [Item(fragment=html.escape(entry[1], quote=False), where="índice") for entry in toc if has_letters(entry[1])]
    title = (document.metadata or {}).get("title") or ""
    title_item = Item(fragment=html.escape(title, quote=False), where="metadatos") if has_letters(title) else None
    translator.run(items + toc_items + ([title_item] if title_item else []))

    assignments: dict[int, str] = {}
    for paragraph in ordered:
        result = paragraph.item.result
        if paragraph.continues is not None:
            a, b = plain(paragraph.fragment), plain(paragraph.continues.fragment)
            first, second = _split_for_pages(plain(result), len(a) / max(1, len(a) + len(b)))
            assignments[id(paragraph)] = html.escape(first, quote=False)
            assignments[id(paragraph.continues)] = html.escape(second, quote=False)
        else:
            assignments[id(paragraph)] = result

    not_fitting = 0
    reduced = 0
    total_pages = len(pages)
    for index, page_paragraphs in enumerate(pages):
        if translator.should_stop():
            raise Cancelled()
        translator.on_stage("maquetando", index + 1, total_pages)
        page = document[index]
        targets = [p for p in page_paragraphs if id(p) in assignments]
        if not targets:
            continue
        backed = any(p.image_backed for p in targets)
        pixmap = page.get_pixmap(dpi=40, colorspace=fitz.csRGB, alpha=False) if backed else None
        for paragraph in targets:
            if paragraph.image_backed:
                area = paragraph.rect + (-1, -1, 1, 1)
                page.add_redact_annot(area, fill=_paper_color(pixmap, area, page.rect))
                continue
            for line in paragraph.lines:
                rect = fitz.Rect(line["bbox"])
                inset = rect.height * 0.15
                page.add_redact_annot(fitz.Rect(rect.x0, rect.y0 + inset, rect.x1, rect.y1 - inset), fill=False)
        page.apply_redactions(
            images=fitz.PDF_REDACT_IMAGE_NONE,
            graphics=fitz.PDF_REDACT_LINE_ART_NONE,
            text=fitz.PDF_REDACT_TEXT_REMOVE,
        )
        shape = page.new_shape()
        for paragraph in targets:
            content = re.sub(r"</?span[^>]*>", "", assignments[id(paragraph)])
            if not content.strip():
                continue
            fits, scale = _place(page, shape, paragraph, content, page_paragraphs)
            if scale < 0.995:
                reduced += 1
            if not fits:
                not_fitting += 1
                translator.issues.append({
                    "tipo": "no cabe", "mensaje": "El texto se ha reducido por debajo del tamaño recomendado.",
                    "lugar": f"página {index + 1}", "original": plain(paragraph.fragment)[:400], "traduccion": plain(content)[:400],
                })
        shape.commit(overlay=True)

    if toc:
        position = 0
        new_toc = []
        for entry in toc:
            if has_letters(entry[1]):
                entry = list(entry)
                entry[1] = plain(toc_items[position].result)
                position += 1
            new_toc.append(entry)
        document.set_toc(new_toc)
    if title_item:
        meta = dict(document.metadata)
        meta["title"] = plain(title_item.result)
        document.set_metadata({k: v for k, v in meta.items() if k in ("title", "author", "subject", "keywords", "creator", "producer", "creationDate", "modDate", "trapped")})

    translator.on_stage("guardando", 0, 1)
    document.save(output, garbage=3, deflate=True)

    _make_bilingual(path, output, bilingual_output)
    return {"no_caben": not_fitting, "reducidos": reduced}


def _make_bilingual(path: Path, output: Path, bilingual_output: Path | None) -> None:
    if bilingual_output is None:
        return
    original = fitz.open(path)
    translated = fitz.open(output)
    combined = fitz.open()
    for number in range(len(original)):
        combined.insert_pdf(original, from_page=number, to_page=number)
        combined.insert_pdf(translated, from_page=number, to_page=number)
    combined.save(bilingual_output, garbage=3, deflate=True)
    combined.close()
    translated.close()
    original.close()
