"""Resúmenes, preguntas al libro y sugerencias de glosario."""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter


WORD = re.compile(r"[^\W\d_]{2,}", re.UNICODE)
SENTENCE = re.compile(r"(?<=[.!?»])\s+(?=[A-ZÁÉÍÓÚÑ«\"“¿¡])")
STOP = set(
    "a al algo ante antes como con contra cual cuando de del desde donde durante e el ella ellas ellos en entre era es esa ese eso esta este esto fue ha han hasta hay la las le les lo los mas más me mi muy no nos o otra otro para pero por porque que qué se sea segun según ser si sí sin sobre su sus tambien también te tiene tu un una uno unos y ya yo "
    "the of and to in that is was he she for it with as his her on be at by this had not are but from or have an they which one you were all we their there been if more when will would who so no what can out about into than them then these its also".split()
)


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text.casefold()) if unicodedata.category(c) != "Mn")


def _tokens(text: str) -> list[str]:
    return [word for word in WORD.findall(_fold(text)) if word not in FOLDED_STOP]


FOLDED_STOP = {_fold(word) for word in STOP}


def suggest_terms(paragraphs: list[dict], limit: int = 12) -> list[dict]:
    counts: Counter[str] = Counter()
    for paragraph in paragraphs:
        for sentence in SENTENCE.split(paragraph["texto"]):
            for match in re.finditer(r"\b[A-ZÁÉÍÓÚÑ][\wáéíóúñ'’-]+(?:\s+(?:de\s+|of\s+|the\s+)?[A-ZÁÉÍÓÚÑ][\wáéíóúñ'’-]+)*", sentence):
                if match.start() == 0 and " " not in match.group(0):
                    continue
                term = match.group(0)
                if _fold(term) not in FOLDED_STOP and len(term) > 3:
                    counts[term] += 1
    return [{"termino": term, "veces": times} for term, times in counts.most_common(limit) if times >= 3]


def retrieve(paragraphs: list[dict], question: str, count: int = 8) -> list[dict]:
    query = set(_tokens(question))
    if not query:
        return []
    tokenized = [Counter(_tokens(p["texto"])) for p in paragraphs]
    documents = Counter(word for tokens in tokenized for word in tokens)
    total = len(paragraphs)
    average = sum(sum(t.values()) for t in tokenized) / max(1, total)
    scored = []
    for index, tokens in enumerate(tokenized):
        length = sum(tokens.values())
        score = 0.0
        for word in query:
            if tokens[word]:
                idf = math.log(1 + (total - documents[word] + 0.5) / (documents[word] + 0.5))
                score += idf * tokens[word] * 2.2 / (tokens[word] + 1.2 * (0.25 + 0.75 * length / max(1, average)))
        if score > 0:
            scored.append((score, index))
    return [paragraphs[index] for _, index in sorted(scored, reverse=True)[:count]]


