"""Fuentes estándar incrustadas una sola vez, con comillas, rayas y acentos correctos."""
from __future__ import annotations

import unicodedata
from functools import lru_cache

import pymupdf as fitz

_FONTS: dict[str, fitz.Font] = {}


def font(key: str) -> fitz.Font:
    if key not in _FONTS:
        _FONTS[key] = fitz.Font(key)
    return _FONTS[key]


@lru_cache(maxsize=200_000)
def unit_width(text: str, key: str) -> float:
    return font(key).text_length(text, fontsize=1.0)


def width(text: str, key: str, size: float) -> float:
    return unit_width(text, key) * size


def internal(key: str) -> str:
    return f"L{key}"


def register(page: fitz.Page, keys) -> None:
    for key in keys:
        page.insert_font(fontname=internal(key), fontbuffer=font(key).buffer)


def has_glyphs(text: str, key: str = "tiro") -> bool:
    current = font(key)
    return all(char.isspace() or current.has_glyph(ord(char)) for char in text)


def safe(text: str, key: str = "tiro") -> str:
    """Sustituye los pocos caracteres que la fuente no tenga."""
    current = font(key)
    text = text.replace("‑", "-").replace(" ", " ")
    output = []
    for char in text:
        if char.isspace() or current.has_glyph(ord(char)):
            output.append(char)
        else:
            plain = "".join(c for c in unicodedata.normalize("NFKD", char) if current.has_glyph(ord(c)) and not unicodedata.combining(c))
            output.append(plain or "?")
    return "".join(output)


def draw(shape, x: float, y: float, text: str, key: str, size: float, color) -> None:
    shape.insert_text((x, y), text, fontname=internal(key), fontsize=size, color=color)
