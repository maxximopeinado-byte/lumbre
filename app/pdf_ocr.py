"""Reconstruye libros escaneados con capa OCR y los compone de nuevo, página a página.

La posición de las palabras del OCR es demasiado irregular para colocar el texto encima.
Aquí se recuperan líneas, párrafos, cabeceras y notas, y cada página se compone limpia,
con un cuerpo de letra uniforme, conservando sus figuras y su numeración.
"""
from __future__ import annotations

import html
import re
import statistics
import unicodedata
from dataclasses import dataclass, field, replace

import pymupdf as fitz

from . import tipos
from .pipeline import Cancelled, Item, Translator, plain

TERMINAL = tuple('.!?:;»"”’)]')
PAGE_NUMBER = re.compile(r"^[\W_]*(\d{1,4}|[ivxlcdmIVXLCDM]{1,7})[\W_]*$")
NOTE_START = re.compile(r"^(\d{1,2}[.)]?|\*|†|‡)\s+\S")
WORD = re.compile(r"[^\W\d_]{2,}", re.UNICODE)
HYPHEN_END = re.compile(r"[^\W\d_][-­]$")
SERIF = {(False, False): "tiro", (True, False): "tibo", (False, True): "tiit", (True, True): "tibi"}
MIN_SCALE = 0.72
TARGET_FLOOR = 0.82


@dataclass
class Line:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    words: list = field(default_factory=list)

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def yc(self) -> float:
        return (self.y0 + self.y1) / 2


@dataclass
class Block:
    kind: str  # cabecera, titulo, parrafo, nota
    text: str
    indent: bool = False
    item: Item | None = None
    result: str = ""
    joins_next: bool = False
    continued: bool = False
    col: int = 0


@dataclass
class PageModel:
    number: int
    rect: fitz.Rect
    lines: list[Line]
    blocks: list[Block] = field(default_factory=list)
    page_label: str = ""
    label_y: float = 0.0
    label_top: bool = False
    top: float = 0.0
    bottom: float = 0.0
    keep: str = ""  # vacía, ruido o figura
    figures: list[fitz.Rect] = field(default_factory=list)
    left: float = 0.0
    right: float = 0.0
    gutter: float = 0.0
    head_y: float = 0.0


@dataclass
class Metrics:
    left: float
    right: float
    pitch: float
    bottom: float
    size: float


def is_scanned(document: fitz.Document) -> bool:
    pages = len(document)
    if not pages:
        return False
    step = max(1, pages // 40)
    sampled = covered = 0
    for index in range(0, pages, step):
        page = document[index]
        area = abs(page.rect) or 1
        sampled += 1
        if any(abs(fitz.Rect(info["bbox"]) & page.rect) >= area * 0.8 for info in page.get_image_info()) and page.get_text("words"):
            covered += 1
    return covered / sampled > 0.5


def _clean(text: str) -> str:
    text = unicodedata.normalize("NFC", text).replace("­", "-")
    text = re.sub(r"\bM\^", "M.", text)
    text = re.sub(r"\s+([,;:.!?])(?=\s|$)", r"\1", text)
    return text


def _lines(page: fitz.Page) -> list[Line]:
    words = [w for w in page.get_text("words") if w[4].strip()]
    if not words:
        return []
    heights = sorted(w[3] - w[1] for w in words)
    median = max(1.0, heights[len(heights) // 2])
    words.sort(key=lambda w: (w[1] + w[3]) / 2)
    rows: list[list[tuple]] = []
    center = -1e9
    for word in words:
        yc = (word[1] + word[3]) / 2
        if rows and abs(yc - center) <= median * 0.48:
            rows[-1].append(word)
            count = len(rows[-1])
            center += (yc - center) / count
        else:
            rows.append([word])
            center = yc
    lines = []
    for row in rows:
        row.sort(key=lambda w: w[0])
        lines.append(Line(
            row[0][0], statistics.median(w[1] for w in row), row[-1][2], statistics.median(w[3] for w in row),
            _clean(" ".join(w[4] for w in row)), row,
        ))
    return lines


def _word_score(text: str) -> float:
    tokens = text.split()
    if not tokens:
        return 0.0
    good = sum(1 for token in tokens if re.fullmatch(r"[\W_]*[^\W\d_]{2,}(?:[-'’][^\W\d_]+)*[\W_\d]*", token))
    return good / len(tokens)


def _metrics(pages: list[PageModel]) -> Metrics:
    lefts, rights, pitches, bottoms = [], [], [], []
    for page in pages:
        width = page.rect.width
        long_lines = [line for line in page.lines if line.x1 - line.x0 > width * 0.5 and _word_score(line.text) > 0.6]
        lefts += [line.x0 for line in long_lines]
        rights += [line.x1 for line in long_lines]
        for a, b in zip(page.lines, page.lines[1:]):
            gap = b.yc - a.yc
            if gap > 0 and a.x1 - a.x0 > width * 0.5 and b.x1 - b.x0 > width * 0.5:
                pitches.append(gap)
        if len(long_lines) > 5:
            bottoms.append(long_lines[-1].y1)
    if not lefts:
        rect = pages[0].rect
        return Metrics(rect.width * 0.1, rect.width * 0.9, 13, rect.height * 0.9, 10)
    left = statistics.median(lefts)
    right = statistics.median(rights)
    pitch = statistics.median(pitches) if pitches else 13.0
    bottom = sorted(bottoms)[int(len(bottoms) * 0.9)] if bottoms else pages[0].rect.height * 0.92
    return Metrics(left, right, pitch, bottom, max(7.5, min(12.0, pitch / 1.2)))


def _join(lines: list[Line]) -> str:
    text = ""
    for line in lines:
        piece = line.text.strip()
        if not text:
            text = piece
        elif HYPHEN_END.search(text) and piece[:1].islower():
            text = text[:-1] + piece
        else:
            text = f"{text} {piece}"
    return re.sub(r"\s+", " ", text).strip()


def _centered(line: Line, m: Metrics) -> bool:
    width = m.right - m.left
    left_gap, right_gap = line.x0 - m.left, m.right - line.x1
    return left_gap > width * 0.08 and right_gap > width * 0.08 and abs(left_gap - right_gap) < width * 0.12


def _page_margins(page: PageModel, m: Metrics) -> Metrics:
    """Los libros alternan márgenes en páginas pares e impares."""
    wide = [l for l in page.lines if l.x1 - l.x0 > (m.right - m.left) * 0.7 and _word_score(l.text) > 0.5]
    book_width = m.right - m.left
    if len(wide) >= 3:
        left = statistics.median(l.x0 for l in wide)
        right = statistics.median(l.x1 for l in wide)
        if right - left < book_width * 0.8:
            right = left + book_width
    elif page.number % 2 == 0 or len(wide) == 0:
        left, right = m.left, m.right
    else:
        left, right = wide[0].x0, wide[0].x0 + book_width
    right = min(right, page.rect.width - 4)
    left = max(left, 4)
    return replace(m, left=left, right=right)


def _find_gutter(lines: list[Line], m: Metrics) -> tuple[int, int, float] | None:
    """Busca un pasillo vertical continuo: la página tiene dos columnas en esa zona."""
    width = m.right - m.left
    best = None
    minimum_gap = max(9.0, width * 0.04)
    for step in range(30, 71, 2):
        x = m.left + width * step / 100
        run_start = None
        misses = 0
        last = None
        for index, line in enumerate(lines + [None]):
            hit = False
            if line is not None:
                ws = line.words
                for a, b in zip(ws, ws[1:]):
                    if a[2] <= x <= b[0] and b[0] - a[2] >= minimum_gap:
                        hit = True
                        break
            if hit:
                if run_start is None:
                    run_start = index
                last = index
                misses = 0
            elif run_start is not None:
                misses += 1
                if misses > 1 or line is None:
                    size = last - run_start + 1
                    if size >= 8 and (best is None or size > best[0]):
                        best = (size, run_start, last, x)
                    run_start = None
                    misses = 0
    return (best[1], best[2], best[3]) if best else None


def _split_columns(lines: list[Line], gutter: float) -> tuple[list[Line], list[Line]]:
    left, right = [], []
    for line in lines:
        a = [w for w in line.words if (w[0] + w[2]) / 2 < gutter]
        b = [w for w in line.words if (w[0] + w[2]) / 2 >= gutter]
        for target, words in ((left, a), (right, b)):
            if words:
                target.append(Line(words[0][0], line.y0, words[-1][2], line.y1, _clean(" ".join(w[4] for w in words)), words))
    return left, right


def _analyze_page(page: PageModel, m: Metrics) -> None:
    lines = page.lines
    if not lines:
        page.keep = "vacía"
        return
    m = _page_margins(page, m)
    page.left, page.right = m.left, m.right
    text = " ".join(line.text for line in lines)
    if _word_score(text) < 0.5 or len(WORD.findall(text)) < 8 and len(lines) < 3 and not any(_centered(l, m) for l in lines):
        page.keep = "ruido"
        return
    # Las motas del escaneado se reconocen como signos sueltos.
    body = [line for line in lines if WORD.search(line.text) or PAGE_NUMBER.match(line.text) or len(line.text) > 3]
    if not body:
        page.keep = "ruido"
        return
    # Número de página, arriba o abajo.
    for index, top_side in ((-1, False), (0, True)):
        if body and PAGE_NUMBER.match(body[index].text) and (body[index].y0 < page.rect.height * 0.12 or body[index].y1 > page.rect.height * 0.85):
            page.page_label = re.sub(r"[^\w]", "", body[index].text)
            page.label_y = body[index].y1
            page.label_top = top_side
            body.pop(index)
            break
    if not body:
        page.keep = "ruido"
        return
    blocks: list[Block] = []
    # Cabecera corriente: título del libro o capítulo, a menudo con el número de página.
    head = body[0]
    gap_after = body[1].y0 - head.y1 if len(body) > 1 else 0
    number_in_head = re.match(r"^(\d{1,4})\s+(.+)$", head.text) or re.match(r"^(.+?)\s+(\d{1,4})$", head.text)
    letters = [c for c in head.text if c.isalpha()]
    mostly_upper = letters and sum(c.isupper() for c in letters) / len(letters) > 0.7
    if head.y0 < page.rect.height * 0.14 and len(head.text) < 70 and gap_after > -2 and mostly_upper and (number_in_head or gap_after > m.pitch * 0.5) and len(body) > 3:
        text = head.text
        if number_in_head:
            first, second = number_in_head.group(1), number_in_head.group(2)
            page.page_label = first if first.isdigit() else second
            text = second if first.isdigit() else first
            page.label_top = True
            page.label_y = head.y1
        page.head_y = head.y1
        blocks.append(Block("cabecera", text))
        body = body[1:]
    page.top = body[0].y0
    page.bottom = max(m.bottom, body[-1].y1)
    # Notas al pie: bloque final separado por un hueco o que empieza por llamada.
    notes_from = len(body)
    for index in range(len(body) - 1, max(0, len(body) - 12), -1):
        if body[index].y0 < page.rect.height * 0.5:
            break
        gap = body[index].y0 - body[index - 1].y1
        if NOTE_START.match(body[index].text) and (gap > m.pitch * 0.25 or body[index].height < m.pitch * 0.72):
            notes_from = index
    notes = body[notes_from:]
    body = body[:notes_from]

    gutter = _find_gutter(body, m)
    if gutter:
        start, end, x = gutter
        page.gutter = x
        left, right = _split_columns(body[start:end + 1], x)
        blocks += _paragraphs(body[:start], m)
        for number, lines_in_col in ((1, left), (2, right)):
            if lines_in_col:
                col_left = min(l.x0 for l in lines_in_col)
                col_right = max(l.x1 for l in lines_in_col)
                for block in _paragraphs(lines_in_col, replace(m, left=col_left, right=col_right)):
                    block.col = number
                    blocks.append(block)
        blocks += _paragraphs(body[end + 1:], m)
    else:
        blocks += _paragraphs(body, m)
    note_block: list[Line] = []
    for line in notes:
        if NOTE_START.match(line.text) and note_block:
            blocks.append(Block("nota", _join(note_block)))
            note_block = []
        note_block.append(line)
    if note_block:
        blocks.append(Block("nota", _join(note_block)))
    page.blocks = blocks


def _paragraphs(body: list[Line], m: Metrics) -> list[Block]:
    blocks: list[Block] = []
    current: list[Line] = []

    def flush(indent: bool = False) -> None:
        nonlocal current
        if current:
            blocks.append(Block("parrafo", _join(current), indent=indent))
            current = []

    pending_indent = False
    width = m.right - m.left
    for index, line in enumerate(body):
        previous = body[index - 1] if index else None
        following = body[index + 1] if index + 1 < len(body) else None
        short = len(line.text) < 70 and (line.x1 - line.x0) < width * 0.75
        heading = short and (_centered(line, m) or (line.text.isupper() and len(line.text) > 3)) and not line.text[:1].islower()
        if heading:
            flush(pending_indent)
            pending_indent = False
            if blocks and blocks[-1].kind == "titulo" and previous is not None and line.y0 - previous.y1 < m.pitch:
                blocks[-1].text = f"{blocks[-1].text} {line.text}"
            else:
                blocks.append(Block("titulo", line.text))
            continue
        starts_new = False
        if previous is not None and current:
            gap = line.yc - previous.yc
            prior_short = previous.x1 < m.right - width * 0.08
            indented = line.x0 > m.left + m.pitch * 0.4 and previous.x0 <= m.left + m.pitch * 0.4
            ended = previous.text.rstrip().endswith(TERMINAL)
            starts_new = gap > m.pitch * 1.55 or (indented and (ended or prior_short)) or (prior_short and ended)
        if starts_new:
            flush(pending_indent)
            pending_indent = line.x0 > m.left + m.pitch * 0.4
        elif not current:
            pending_indent = line.x0 > m.left + m.pitch * 0.4
        current.append(line)
        if following is None:
            flush(pending_indent)
    flush(pending_indent)
    return blocks


def _figures(page_obj: fitz.Page, page: PageModel, m: Metrics) -> None:
    """Busca tinta fuera del texto: ilustraciones que hay que conservar."""
    pix = page_obj.get_pixmap(dpi=36, colorspace=fitz.csGRAY, alpha=False)
    width, height = pix.width, pix.height
    sx, sy = width / page.rect.width, height / page.rect.height
    samples = bytes(pix.samples)
    if not samples:
        return
    background = sorted(samples[::41])[len(samples[::41]) // 2]
    limit = background - 60
    dark = samples.translate(bytes(1 if value < limit else 0 for value in range(256)))
    outside = bytearray(b"\x01" * (width * height))
    for line in page.lines:
        x0, x1 = max(0, int((line.x0 - 3) * sx)), min(width, int((line.x1 + 3) * sx) + 1)
        y0, y1 = max(0, int((line.y0 - 2) * sy)), min(height, int((line.y1 + 2) * sy) + 1)
        for y in range(y0, y1):
            outside[y * width + x0: y * width + x1] = bytes(x1 - x0)
    margin_x, margin_y = int(width * 0.04), int(height * 0.03)
    threshold = max(4, int((width - 2 * margin_x) * 0.05))
    ink_rows: list[int] = []
    masks: dict[int, int] = {}
    for y in range(margin_y, height - margin_y):
        start = y * width
        mask = int.from_bytes(dark[start + margin_x: start + width - margin_x], "big") & int.from_bytes(outside[start + margin_x: start + width - margin_x], "big")
        masks[y] = mask
        if mask and mask.bit_count() >= threshold:
            ink_rows.append(y)
    ink_rows.append(10 ** 9)
    run_start = previous = None
    columns = width - 2 * margin_x
    for y in ink_rows:
        if run_start is None:
            run_start = previous = y
            continue
        if y - previous > 1:
            if (previous - run_start + 1) / sy > max(40, m.pitch * 3.5):
                union = 0
                for row in range(run_start, previous + 1):
                    union |= masks[row]
                if union:
                    # Cada píxel es un byte: se traduce la posición de los bits a columnas.
                    text = union.to_bytes(columns, "big")
                    first = next(i for i, value in enumerate(text) if value)
                    last = columns - 1 - next(i for i, value in enumerate(reversed(text)) if value)
                    page.figures.append(fitz.Rect((first + margin_x) / sx, run_start / sy, (last + margin_x + 1) / sx, (previous + 1) / sy) + (-3, -3, 3, 3))
            run_start = y
        previous = y


def build_model(document: fitz.Document, figures: bool = True, on_step=None) -> tuple[list[PageModel], Metrics]:
    pages = [PageModel(i, fitz.Rect(p.rect), _lines(p)) for i, p in enumerate(document)]
    metrics = _metrics(pages)
    for index, (model, page) in enumerate(zip(pages, document)):
        _analyze_page(model, metrics)
        if figures and model.keep != "vacía":
            if on_step:
                on_step(index + 1, len(pages))
            _figures(page, model, metrics)
            if index % 8 == 0:
                # Las páginas escaneadas pesan mucho: no se conservan en memoria.
                fitz.TOOLS.store_shrink(100)
            if model.figures and sum(abs(f) for f in model.figures) > abs(model.rect) * 0.55:
                model.keep = "figura"
    _mark_continuations(pages)
    return pages, metrics


def _mark_continuations(pages: list[PageModel]) -> None:
    usable = [p for p in pages if p.blocks and not p.keep]
    for current, following in zip(usable, usable[1:]):
        if following.number != current.number + 1:
            continue
        ends = [b for b in current.blocks if b.kind == "parrafo" and not b.col]
        starts = [b for b in following.blocks if b.kind == "parrafo" and not b.col]
        if not ends or not starts:
            continue
        first_other = next((b for b in following.blocks if b.kind != "cabecera"), None)
        if first_other is not starts[0]:
            continue
        if not ends[-1].text.endswith(TERMINAL) or starts[0].text[:1].islower():
            ends[-1].joins_next = True
            starts[0].continued = True
            starts[0].indent = False


def analyze(document: fitz.Document) -> dict:
    pages, _ = build_model(document, figures=False)
    samples = [
        {"lugar": f"página {p.number + 1}", "texto": block.text}
        for p in pages for block in p.blocks
    ]
    kept = sum(1 for p in pages if p.keep in ("ruido", "figura"))
    empty = sum(1 for p in pages if p.keep == "vacía")
    warnings = [
        "Es un libro escaneado. Lumbre reconstruirá el texto reconocido por OCR y compondrá cada página de nuevo; "
        "los errores de ese reconocimiento pueden pasar a la traducción."
    ]
    if kept or empty:
        warnings.append(f"{kept + empty} páginas sin texto de libro (láminas, sellos o en blanco) quedarán como están.")
    return {"parrafos": samples, "avisos": warnings}


# Composición -----------------------------------------------------------------


def _hyphenator(language: str):
    try:
        import pyphen
    except ImportError:
        return None
    code = {"es": "es", "en": "en_GB", "fr": "fr", "de": "de_DE", "it": "it_IT", "pt": "pt_PT", "ca": "ca", "nl": "nl_NL"}.get(language)
    return pyphen.Pyphen(lang=code) if code and code in pyphen.LANGUAGES else None


def _safe(text: str) -> str:
    return tipos.safe(text)


class Composer:
    def __init__(self, hyphenator, size: float, leading: float = 1.2) -> None:
        self.hyphenator = hyphenator
        self.size = size
        self.leading = leading

    def width(self, text: str, font: str) -> float:
        return tipos.unit_width(text, font)

    def wrap(self, text: str, font: str, size: float, width: float, first_indent: float = 0.0) -> list[list[str]]:
        lines: list[list[str]] = [[]]
        used = first_indent
        space = self.width(" ", font) * size
        limit = width
        for word in text.split():
            length = self.width(word, font) * size
            gap = space if lines[-1] else 0.0
            if used + gap + length <= limit:
                lines[-1].append(word)
                used += gap + length
                continue
            placed = False
            if self.hyphenator and len(word) >= 6 and True:
                core = re.match(r"^([^\w]*)([\w-]+)([^\w]*)$", word, re.UNICODE)
                if core and core.group(2).isalpha():
                    for head, tail in self.hyphenator.iterate(core.group(2)):
                        piece = core.group(1) + head + "-"
                        piece_length = self.width(piece, font) * size
                        if used + gap + piece_length <= limit and len(head) >= 2 and len(tail) >= 2:
                            lines[-1].append(piece)
                            rest = tail + core.group(3)
                            lines.append([rest])
                            used = self.width(rest, font) * size
                            placed = True
                            break
            if not placed:
                if not lines[-1]:
                    lines.pop()
                lines.append([word])
                used = length
        return lines

    def draw(self, shape, lines: list[list[str]], x: float, y: float, width: float, font: str, size: float, color, align: str, first_indent: float = 0.0) -> float:
        space = self.width(" ", font) * size
        for index, words in enumerate(lines):
            indent = first_indent if index == 0 else 0.0
            natural = sum(self.width(word, font) * size for word in words)
            gaps = len(words) - 1
            gap = space
            start = x + indent
            if align == "center":
                start = x + max(0.0, (width - natural - gaps * space) / 2)
            elif align == "justify" and gaps and index < len(lines) - 1:
                gap = (width - indent - natural) / gaps
                gap = min(gap, space * 2.4) if gap > space * 2.4 else max(gap, space * 0.75)
            cursor = start
            for word in words:
                tipos.draw(shape, cursor, y, word, font, size, color)
                cursor += self.width(word, font) * size + gap
            y += size * self.leading
        return y


def _layout_page(model: PageModel, m: Metrics, composer: Composer, scale: float):
    """Devuelve las piezas ya partidas en líneas y si caben en la página."""
    size = m.size * scale
    width = model.right - model.left
    column = (width - size * 1.6) / 2
    pieces = []
    full_height = 0.0
    col_heights = [0.0, 0.0, 0.0]
    note_height = 0.0
    for block in model.blocks:
        text = _safe(block.result or block.text)
        if not text.strip():
            continue
        wide = column if block.col else width
        if block.kind == "cabecera":
            pieces.append((block, [text.split()], SERIF[(False, True)], size * 0.8, "center", 0.0, 0.0, 0.0))
            continue
        if block.kind == "titulo":
            font, block_size, align, indent = SERIF[(True, False)], size * (1.0 if block.col else 1.12), "center", 0.0
        elif block.kind == "nota":
            font, block_size, align, indent = SERIF[(False, False)], size * 0.84, "justify", 0.0
        else:
            font, block_size, align, indent = SERIF[(False, False)], size * (0.92 if block.col else 1.0), "justify", size * 1.2 if block.indent else 0.0
        lines = composer.wrap(text, font, block_size, wide, indent)
        height = len(lines) * block_size * composer.leading
        before = size * 0.75 if block.kind == "titulo" else (size * 0.12 if block.kind == "parrafo" and not block.indent else 0.0)
        pieces.append((block, lines, font, block_size, align, indent, height, before))
        if block.kind == "nota":
            note_height += height + block_size * 0.25
        elif block.col:
            col_heights[block.col] += height + before
        else:
            full_height += height + before
    figures_height = sum(f.height + size for f in model.figures)
    rule = size * 0.9 if note_height else 0.0
    available = model.bottom - model.top
    body = full_height + max(col_heights)
    return pieces, body + figures_height + note_height + rule <= available, note_height


def translate(document: fitz.Document, output, translator: Translator, language: str) -> dict:
    pages, metrics = build_model(document, on_step=lambda done, total: translator.on_stage("preparando", done, total))
    items: list[Item] = []
    groups: list[list[Block]] = []
    for model in pages:
        for block in model.blocks:
            if block.continued:
                continue
            group = [block]
            if block.joins_next:
                # El párrafo puede seguir en varias páginas consecutivas.
                index = pages.index(model) + 1
                while index < len(pages):
                    candidate = next((b for b in pages[index].blocks if b.continued), None)
                    if candidate is None:
                        break
                    group.append(candidate)
                    if not candidate.joins_next:
                        break
                    index += 1
            source = " ".join(b.text for b in group)
            if HYPHEN_END.search(group[0].text) and len(group) > 1:
                source = _join([Line(0, 0, 0, 0, b.text) for b in group])
            if not WORD.search(source):
                continue
            block.item = Item(fragment=html.escape(source, quote=False), where=f"página {model.number + 1}")
            items.append(block.item)
            groups.append(group)
    toc = document.get_toc(simple=False)
    toc_items = [Item(fragment=html.escape(entry[1], quote=False), where="índice") for entry in toc if WORD.search(entry[1])]
    translator.run(items + toc_items)

    for group in groups:
        result = plain(group[0].item.result)
        if len(group) == 1:
            group[0].result = result
            continue
        total = sum(len(b.text) for b in group) or 1
        words = result.split()
        position = 0
        for index, block in enumerate(group):
            if index == len(group) - 1:
                block.result = " ".join(words[position:])
                break
            share = len(block.text) / total
            wanted = max(1, round(len(words) * share))
            block.result = " ".join(words[position:position + wanted])
            position += wanted

    composer = Composer(_hyphenator(language), metrics.size)
    # Primero se calcula la escala que necesita cada página para usar una letra uniforme.
    needs = {}
    active = [m for m in pages if not m.keep and m.blocks]
    for index, model in enumerate(active):
        if translator.should_stop():
            raise Cancelled()
        scale = 1.0
        while scale > MIN_SCALE:
            if _layout_page(model, metrics, composer, scale)[1]:
                break
            scale = round(scale - 0.01, 3)
        needs[model.number] = scale
        translator.on_stage("maquetando", index // 2 + 1, len(active))
    ordered = sorted(needs.values())
    uniform = max(TARGET_FLOOR, ordered[int(len(ordered) * 0.08)]) if ordered else 1.0

    paper = _paper(document, active)
    ink = (0.13, 0.11, 0.1)
    reduced = not_fitting = 0
    for index, model in enumerate(active):
        if translator.should_stop():
            raise Cancelled()
        translator.on_stage("maquetando", len(active) // 2 + index // 2 + 1, len(active))
        page = document[model.number]
        if index % 8 == 0:
            fitz.TOOLS.store_shrink(100)
        crops = [(f, page.get_pixmap(dpi=170, clip=f, alpha=False).tobytes("jpeg", jpg_quality=82)) for f in model.figures]
        page.add_redact_annot(page.rect, fill=paper)
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_REMOVE, graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED, text=fitz.PDF_REDACT_TEXT_REMOVE)
        scale = min(uniform, needs[model.number])
        if scale < uniform:
            reduced += 1
        pieces, fits, note_height = _layout_page(model, metrics, composer, scale)
        if not fits:
            not_fitting += 1
            translator.issues.append({
                "tipo": "no cabe", "gravedad": "alta", "mensaje": "La página no cabe ni con la letra mínima.",
                "lugar": f"página {model.number + 1}", "original": model.blocks[0].text[:300], "traduccion": (model.blocks[0].result or "")[:300],
            })
        tipos.register(page, {piece[2] for piece in pieces} | {SERIF[(False, False)]})
        shape = page.new_shape()
        y = model.top
        size = metrics.size * scale
        width = model.right - model.left
        figures = sorted(model.figures, key=lambda f: f.y0)
        notes = [p for p in pieces if p[0].kind == "nota"]
        column = (width - size * 1.6) / 2
        col_y = {1: 0.0, 2: 0.0}
        for block, lines, font, block_size, align, indent, height, before in pieces:
            if block.kind == "nota":
                continue
            if block.kind == "cabecera":
                head_y = model.head_y or model.top - size
                composer.draw(shape, lines, model.left, head_y, width, font, block_size, ink, "center")
                continue
            if block.col:
                if not col_y[block.col]:
                    col_y[block.col] = y
                col_y[block.col] += before
                x = model.left if block.col == 1 else model.left + column + size * 1.6
                composer.draw(shape, lines, x, col_y[block.col] + block_size * 0.95, column, font, block_size, ink, align, indent)
                col_y[block.col] += height
                continue
            if col_y[1] or col_y[2]:
                y = max(y, col_y[1], col_y[2]) + size * 0.4
                col_y = {1: 0.0, 2: 0.0}
            y += before
            for figure in list(figures):
                if figure.y0 <= y + height:
                    if y + height > figure.y0 and y < figure.y1:
                        y = figure.y1 + size * 0.6
                    figures.remove(figure)
            composer.draw(shape, lines, model.left, y + block_size * 0.95, width, font, block_size, ink, align, indent)
            y += height
            if block.kind == "titulo":
                y += size * 0.45
        y = max(y, col_y[1], col_y[2])
        if notes:
            note_y = max(y + size * 0.6, model.bottom - note_height)
            shape.draw_line((model.left, note_y - size * 0.25), (model.left + width * 0.3, note_y - size * 0.25))
            shape.finish(color=ink, width=0.4)
            for block, lines, font, block_size, align, indent, height, before in notes:
                composer.draw(shape, lines, model.left, note_y + block_size * 0.95, width, font, block_size, ink, align, indent)
                note_y += height + block_size * 0.25
        if model.page_label:
            label = model.page_label
            label_y = model.label_y if model.label_top else min(model.rect.height - 6, max(model.label_y, model.bottom + size * 1.1))
            length = tipos.width(label, SERIF[(False, False)], size * 0.9)
            label_x = model.left + (width - length) / 2
            if model.label_top:
                label_x = model.right - length if model.number % 2 == 0 else model.left
            tipos.draw(shape, label_x, label_y, label, SERIF[(False, False)], size * 0.9, ink)
        shape.commit(overlay=True)
        for rect, data in crops:
            page.insert_image(rect, stream=data)

    if toc:
        position = 0
        new_toc = []
        for entry in toc:
            if WORD.search(entry[1]):
                entry = list(entry)
                entry[1] = plain(toc_items[position].result)
                position += 1
            new_toc.append(entry)
        document.set_toc(new_toc)
    translator.on_stage("guardando", 0, 1)
    document.save(output, garbage=4, deflate=True)
    kept = [m.number + 1 for m in pages if m.keep in ("ruido", "figura")]
    return {"no_caben": not_fitting, "reducidos": reduced, "paginas_conservadas": kept, "cuerpo": round(metrics.size * uniform, 1), "metodo": "escaneado"}


def _paper(document: fitz.Document, active: list[PageModel]) -> tuple[float, float, float]:
    samples = []
    for model in active[:: max(1, len(active) // 12)]:
        pix = document[model.number].get_pixmap(dpi=20, colorspace=fitz.csRGB, alpha=False)
        data = pix.samples
        samples.append(tuple(statistics.median(data[i::3 * 7]) for i in range(3)))
    if not samples:
        return (0.99, 0.98, 0.96)
    base = [statistics.median(s[i] for s in samples) / 255 for i in range(3)]
    return tuple(min(1.0, value * 0.25 + 0.75) for value in base)
