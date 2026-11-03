"""Reparte fragmentos entre el motor y la memoria, y comprueba cada resultado."""
from __future__ import annotations

import html
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Callable

from . import calidad
from .engines import Engine
from .store import Memory

TAG = re.compile(r"<[^>]+>")
NOTRANSLATE = '<span translate="no" class="notranslate">'
LETTER = re.compile(r"[^\W\d_]", re.UNICODE)
NUMBER = re.compile(r"\d(?:[\d.,\s]*\d)?")


class Cancelled(Exception):
    pass


def plain(fragment: str) -> str:
    return html.unescape(TAG.sub("", fragment)).strip()


def has_letters(text: str) -> bool:
    return bool(LETTER.search(text))


def parse_glossary(raw: str) -> list[tuple[str, str]]:
    terms = []
    for line in (raw or "").splitlines():
        if "=" in line:
            source, target = line.split("=", 1)
            if source.strip() and target.strip():
                terms.append((source.strip(), target.strip()))
    return sorted(terms, key=lambda pair: -len(pair[0]))


def protect_terms(fragment: str, glossary: list[tuple[str, str]]) -> str:
    if not glossary:
        return fragment
    lookup = {source.casefold(): target for source, target in glossary}
    pattern = re.compile(
        r"(?<!\w)(" + "|".join(re.escape(source) for source, _ in glossary) + r")(?!\w)", re.I
    )

    def replace(match: re.Match) -> str:
        return f"{NOTRANSLATE}{html.escape(lookup[match.group(1).casefold()], quote=False)}</span>"

    parts = re.split(r"(<[^>]+>)", fragment)
    return "".join(part if part.startswith("<") else pattern.sub(replace, part) for part in parts)


@dataclass
class Item:
    fragment: str
    where: str = ""
    result: str = ""
    extra_checks: dict = field(default_factory=dict)
    kind: str = "parrafo"

    @property
    def text(self) -> str:
        return plain(self.fragment)


def check_pair(item: Item, translated_text: str, source: str, target: str, glossary) -> list[dict]:
    return calidad.check(item.text, translated_text, item.where, source, target, glossary)


class Translator:
    def __init__(
        self,
        engine: Engine,
        memory: Memory,
        source: str,
        target: str,
        glossary: list[tuple[str, str]] | None = None,
        on_progress: Callable[[int, int], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        on_stage: Callable[[str, int, int], None] | None = None,
        reuse_any: bool = False,
    ) -> None:
        self.engine = engine
        self.memory = memory
        self.source = source
        self.target = target
        self.glossary = glossary or []
        self.on_progress = on_progress or (lambda done, total: None)
        self.should_stop = should_stop or (lambda: False)
        self.on_stage = on_stage or (lambda stage, done, total: None)
        self.reuse_any = reuse_any
        self.ignore_memory = False
        self.pages: set[int] | None = None
        self.segments: list[dict] = []
        self.issues: list[dict] = []
        self.reused = 0
        self.sent = 0
        self._window: list[tuple[float, int]] = []

    def _pace(self, characters: int) -> None:
        limit = self.engine.chars_per_minute
        if not limit:
            return
        while True:
            now = time.monotonic()
            self._window = [(t, c) for t, c in self._window if now - t < 60]
            if sum(c for _, c in self._window) + characters <= limit or not self._window:
                break
            if self.should_stop():
                raise Cancelled()
            time.sleep(1)
        self._window.append((time.monotonic(), characters))

    def run(self, items: list[Item]) -> None:
        name = self.engine.memory_name
        unique: dict[str, dict] = {}
        for item in items:
            protected = protect_terms(item.fragment, self.glossary)
            entry = unique.setdefault(protected, {
                "items": [], "key": Memory.key(name, self.source, self.target, protected),
                "any": Memory.key("*", "*", self.target, protected),
            })
            entry["items"].append(item)
        total = sum(len(plain(f)) for f in unique)
        done = 0
        pending: list[str] = []
        for fragment, entry in unique.items():
            cached = None if self.ignore_memory else self.memory.get(entry["key"])
            if cached is None and self.reuse_any and not self.ignore_memory:
                cached = self.memory.get(entry["any"])
            if cached is not None:
                entry["result"] = cached
                done += len(plain(fragment))
                self.reused += len(plain(fragment))
            else:
                pending.append(fragment)
        self.on_progress(done, total)

        batch: list[str] = []
        batch_size = 0
        previous = ""

        def flush() -> None:
            nonlocal batch, batch_size, done, previous
            if not batch:
                return
            if self.should_stop():
                raise Cancelled()
            self._pace(batch_size)
            results = self.engine.translate(batch, self.source, self.target, previous[-400:])
            pairs = []
            for fragment, result in zip(batch, results):
                unique[fragment]["result"] = result
                pairs.append((unique[fragment]["key"], result))
                pairs.append((unique[fragment]["any"], result))
                done += len(plain(fragment))
            sent = sum(len(plain(f)) for f in batch)
            self.sent += sent
            self.memory.put_many(pairs)
            self.memory.add_usage(self.engine.id, sent)
            previous = plain(batch[-1])
            batch, batch_size = [], 0
            self.on_progress(done, total)

        for fragment in pending:
            size = len(fragment.encode("utf-8"))
            if batch and (len(batch) >= self.engine.max_items or batch_size + size > self.engine.max_chars):
                flush()
            batch.append(fragment)
            batch_size += size
        flush()

        seen: set[str] = set()
        for item in items:
            protected = protect_terms(item.fragment, self.glossary)
            if protected not in seen:
                seen.add(protected)
                entry = unique[protected]
                self.segments.append({
                    "original": plain(re.sub(r'<span\b[^>]*notranslate[^>]*>#</span>', "", item.fragment)), "fragmento": protected, "traduccion": entry["result"], "clave": entry["key"],
                    "clave_general": entry["any"], "lugar": item.where, "tipo": item.kind,
                })
        for fragment, entry in unique.items():
            for item in entry["items"]:
                item.result = entry["result"]
                self.issues.extend(check_pair(item, plain(item.result), self.source, self.target, self.glossary))


def normalize(text: str) -> str:
    return unicodedata.normalize("NFC", text)
