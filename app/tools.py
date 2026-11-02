"""Resúmenes, preguntas al libro y sugerencias de glosario."""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter

from .engines import claude_message

WORD = re.compile(r"[^\W\d_]{2,}", re.UNICODE)
SENTENCE = re.compile(r"(?<=[.!?»])\s+(?=[A-ZÁÉÍÓÚÑ«\"“¿¡])")
STOP = set(
    "a al algo ante antes como con contra cual cuando de del desde donde durante e el ella ellas ellos en entre era es esa ese eso esta este esto fue ha han hasta hay la las le les lo los mas más me mi muy no nos o otra otro para pero por porque que qué se sea segun según ser si sí sin sobre su sus tambien también te tiene tu un una uno unos y ya yo "
    "the of and to in that is was he she for it with as his her on be at by this had not are but from or have an they which one you were all we their there been if more when will would who so no what can out about into than them then these its also".split()
)
LEVELS = {
    "breve": ("5 ideas principales", 5),
    "media": ("10 ideas principales, cada una con una frase de explicación", 10),
    "extensa": ("un resumen por grandes partes del libro y 15 ideas principales", 15),
}


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text.casefold()) if unicodedata.category(c) != "Mn")


def _tokens(text: str) -> list[str]:
    return [word for word in WORD.findall(_fold(text)) if word not in FOLDED_STOP]


FOLDED_STOP = {_fold(word) for word in STOP}


def extractive_summary(paragraphs: list[dict], count: int) -> list[dict]:
    sentences: list[tuple[str, str]] = []
    for paragraph in paragraphs:
        for sentence in SENTENCE.split(paragraph["texto"]):
            if 50 <= len(sentence) <= 320 and sentence.rstrip()[-1:] in ".!?»":
                sentences.append((sentence.strip(), paragraph["lugar"]))
    if not sentences:
        return []
    tokenized = [_tokens(sentence) for sentence, _ in sentences]
    documents = Counter(word for tokens in tokenized for word in set(tokens))
    total = len(sentences)
    scored = []
    for index, tokens in enumerate(tokenized):
        if len(tokens) < 5:
            continue
        counts = Counter(tokens)
        weight = sum(math.log(1 + documents[w]) * math.log(1 + total / documents[w]) * min(c, 2) for w, c in counts.items())
        scored.append((weight / math.sqrt(len(tokens)), index))
    chosen: list[int] = []
    seen: list[set[str]] = []
    for _, index in sorted(scored, reverse=True):
        words = set(tokenized[index])
        if any(len(words & other) / max(1, len(words | other)) > 0.35 for other in seen):
            continue
        chosen.append(index)
        seen.append(words)
        if len(chosen) >= count:
            break
    return [{"texto": sentences[i][0], "lugar": sentences[i][1]} for i in sorted(chosen)]


def _chunks(paragraphs: list[dict], limit: int) -> list[str]:
    chunks, current, size = [], [], 0
    for paragraph in paragraphs:
        line = f"[{paragraph['lugar']}] {paragraph['texto']}"
        if current and size + len(line) > limit:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line)
    if current:
        chunks.append("\n".join(current))
    return chunks


def claude_summary(paragraphs: list[dict], level: str, key: str, model: str) -> str:
    wanted, _ = LEVELS.get(level, LEVELS["media"])
    notes = []
    for chunk in _chunks(paragraphs, 120_000):
        notes.append(claude_message(
            key, model,
            "Extrae en castellano los argumentos, datos y conclusiones de este tramo de un ensayo. "
            "Indica entre corchetes de qué capítulo o página sale cada punto. No añadas nada que no esté en el texto.\n\n" + chunk,
            max_tokens=3000,
        ))
    return claude_message(
        key, model,
        f"Con estas notas de un libro, escribe en castellano {wanted}. Usa Markdown, sé fiel al texto y "
        "conserva la referencia de capítulo o página. No añadas opiniones ni información externa.\n\n" + "\n\n".join(notes),
        max_tokens=4000,
    )


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


def claude_answer(question: str, passages: list[dict], key: str, model: str) -> str:
    context = "\n\n".join(f"[{p['lugar']}] {p['texto']}" for p in passages)
    return claude_message(
        key, model,
        "Responde en castellano usando solo estos fragmentos de un libro. Cita entre corchetes el capítulo o la página. "
        "Si los fragmentos no bastan, dilo claramente.\n\n"
        f"Fragmentos:\n{context}\n\nPregunta: {question}",
        max_tokens=1500,
    )
