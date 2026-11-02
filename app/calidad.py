"""Control de calidad: avisos con gravedad y comprobación de las páginas compuestas."""
from __future__ import annotations

import re
import unicodedata

import pymupdf as fitz

NUMBER = re.compile(r"\d(?:[\d.,\s]*\d)?")
NAME = re.compile(r"(?<=[a-záéíóúñ,;] )[A-ZÁÉÍÓÚÑ][a-záéíóúñ]{3,}")
STOPWORDS = {
    "en": set("the of and that with from which this were have been their would there about".split()),
    "fr": set("les des une dans pour avec que qui sur est".split()),
    "de": set("und der die das nicht mit ist eine von".split()),
    "it": set("della degli che per con una sono gli".split()),
    "pt": set("uma para com que dos das não são".split()),
}
GRAVITY = {"vacío": "alta", "sin traducir": "alta", "no cabe": "alta", "solapamiento": "alta", "fuera de página": "alta", "glosario": "alta", "lengua original": "alta", "formato": "media", "cifras": "media", "longitud": "media", "nombre": "baja", "cifra menor": "baja", "frase": "baja"}


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text.casefold()) if unicodedata.category(c) != "Mn")


def _digits(text: str) -> list[str]:
    return sorted(re.sub(r"\D", "", found) for found in NUMBER.findall(text))


def looks_like_text(text: str) -> bool:
    tokens = text.split()
    if not tokens:
        return False
    good = sum(1 for token in tokens if re.fullmatch(r"[\W_]*[^\W\d_]{2,}[\W_]*", token))
    return good / len(tokens) >= 0.55


def check(original: str, translated: str, where: str, source: str, target: str, glossary) -> list[dict]:
    issues: list[dict] = []

    def add(kind: str, message: str) -> None:
        issues.append({
            "tipo": kind, "gravedad": GRAVITY.get(kind, "media"), "mensaje": message, "lugar": where,
            "original": original[:400], "traduccion": translated[:400],
        })

    if not translated.strip():
        add("vacío", "La traducción está vacía.")
        return issues
    readable = looks_like_text(original)
    if source != target and readable and _fold(translated.strip()) == _fold(original.strip()) and len(original.split()) >= 4:
        add("sin traducir", "El texto es igual al original.")
    words = re.findall(r"[^\W\d_]+", _fold(translated))
    if readable and len(words) >= 8:
        for language, stop in STOPWORDS.items():
            if language != target and sum(1 for w in words if w in stop) / len(words) > 0.16:
                add("lengua original", f"Parece quedar texto en otro idioma ({language}).")
                break
    a, b = _digits(original), _digits(translated)
    if a != b and readable:
        different = (set(a) ^ set(b))
        major = [d for d in different if len(d) >= 2]
        if major:
            add("cifras", "Las cifras no coinciden: " + ", ".join(sorted(major)[:6]) + ".")
        elif different:
            add("cifra menor", "Cambia una cifra suelta o una llamada de nota.")
    if len(original) >= 80:
        ratio = len(translated) / max(1, len(original))
        if ratio < 0.55 or ratio > 2.2:
            add("longitud", f"La longitud cambia demasiado ({ratio:.1f} veces el original).")
    folded = _fold(original)
    result = _fold(translated)
    for term_source, term_target in glossary:
        if re.search(r"(?<!\w)" + re.escape(_fold(term_source)) + r"(?!\w)", folded) and _fold(term_target) not in result:
            add("glosario", f"Falta el término «{term_target}».")
    missing = sorted({name for name in NAME.findall(original) if _fold(name) not in result and _fold(name)[:-1] not in result})
    if missing and readable and len(missing) <= 3 and all(len(name) >= 6 for name in missing):
        add("nombre", "Revisa estos nombres: " + ", ".join(missing) + ".")
    if readable and original.rstrip()[-1:] in ".?!" and translated.rstrip()[-1:] not in ".?!»\"”)…":
        add("frase", "El párrafo traducido no termina como el original.")
    return issues


def verify_pdf(path, on_step=None) -> list[dict]:
    """Abre el PDF final y busca palabras solapadas o fuera de la página."""
    issues: list[dict] = []
    document = fitz.open(path)
    try:
        for index, page in enumerate(document):
            if on_step:
                on_step(index + 1, len(document))
            words = page.get_text("words")
            rect = page.rect
            outside = [w for w in words if w[0] < rect.x0 - 1 or w[2] > rect.x1 + 1 or w[1] < rect.y0 - 1 or w[3] > rect.y1 + 1]
            if outside:
                issues.append(_layout("fuera de página", f"{len(outside)} palabras se salen de la página.", index, " ".join(w[4] for w in outside[:12])))
            ordered = sorted(words, key=lambda w: (round(w[1]), w[0]))
            collisions = []
            for i, a in enumerate(ordered):
                for b in ordered[i + 1: i + 30]:
                    if b[1] > a[3]:
                        break
                    width = min(a[2], b[2]) - max(a[0], b[0])
                    height = min(a[3], b[3]) - max(a[1], b[1])
                    smaller = min(a[3] - a[1], b[3] - b[1])
                    if width > 1.5 and height > smaller * 0.55:
                        collisions.append((a[4], b[4]))
            if len(collisions) >= 2:
                sample = ", ".join(f"{x}/{y}" for x, y in collisions[:6])
                issues.append(_layout("solapamiento", f"Hay {len(collisions)} palabras superpuestas.", index, sample))
    finally:
        document.close()
    return issues


def _layout(kind: str, message: str, index: int, sample: str) -> dict:
    return {"tipo": kind, "gravedad": GRAVITY[kind], "mensaje": message, "lugar": f"página {index + 1}", "original": "", "traduccion": sample[:300], "pagina": index + 1}


def summary(issues: list[dict]) -> dict:
    counts = {"alta": 0, "media": 0, "baja": 0}
    for issue in issues:
        counts[issue.get("gravedad", "media")] += 1
    return counts
