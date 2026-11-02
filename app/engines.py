"""Motores de traducción intercambiables.

Todos reciben fragmentos HTML con etiquetas <span> y <b>/<i> y deben devolverlas.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Callable

import requests

TIMEOUT = 120
MAX_RETRIES = 5

LANGUAGES = {
    "auto": "Detectar automáticamente",
    "es": "Castellano",
    "en": "Inglés",
    "fr": "Francés",
    "de": "Alemán",
    "it": "Italiano",
    "pt": "Portugués",
    "ca": "Catalán",
    "nl": "Neerlandés",
}


class EngineError(Exception):
    """kind: quota, auth, rate, network o bad."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def _request(method: str, url: str, classify: Callable[[requests.Response], EngineError | None], **kwargs) -> requests.Response:
    last_error = "sin respuesta"
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.request(method, url, timeout=TIMEOUT, **kwargs)
        except requests.RequestException as exc:
            last_error = f"No se pudo conectar: {type(exc).__name__}"
            time.sleep(min(2 ** attempt, 30))
            continue
        if response.status_code == 200:
            return response
        error = classify(response)
        if error:
            raise error
        if response.status_code == 429 or response.status_code >= 500:
            last_error = f"El servicio respondió {response.status_code}"
            wait = response.headers.get("Retry-After", "")
            delay = int(wait) if wait.isdigit() else 2 ** (attempt + 1)
            time.sleep(min(delay, 60))
            continue
        raise EngineError("bad", f"El servicio rechazó la petición ({response.status_code}): {response.text[:200]}")
    raise EngineError("network", f"{last_error}. Puedes reanudar: lo traducido ya está guardado.")


class Engine:
    id = "base"
    label = "Base"
    free_quota = 0
    max_items = 50
    max_chars = 20_000
    chars_per_minute = 0

    @property
    def memory_name(self) -> str:
        return self.id

    def translate(self, fragments: list[str], source: str, target: str, context: str = "") -> list[str]:
        raise NotImplementedError

    def check(self) -> str:
        result = self.translate(["Good morning."], "en", "es")
        return result[0]

    def remaining_quota(self) -> int | None:
        return None


class DemoEngine(Engine):
    """Comprueba libros y maquetación sin cuentas. No traduce de verdad."""

    id = "prueba"
    label = "Prueba sin clave"

    def translate(self, fragments, source, target, context=""):
        return [self._fake(fragment) for fragment in fragments]

    @staticmethod
    def _fake(fragment: str) -> str:
        parts = re.split(r"(<[^>]+>)", fragment)
        protected = 0
        depth: list[bool] = []
        output = []
        for part in parts:
            if part.startswith("<") and part.endswith(">"):
                if part.startswith("</"):
                    if depth and depth.pop():
                        protected -= 1
                else:
                    is_protected = "notranslate" in part
                    depth.append(is_protected)
                    protected += 1 if is_protected else 0
                output.append(part)
            elif protected:
                output.append(part)
            else:
                output.append(re.sub(r"[^\W\d_]{5,}", lambda m: m.group(0) + "a", part))
        return "".join(output)


class DeepLEngine(Engine):
    id = "deepl"
    label = "DeepL"
    free_quota = 500_000
    max_items = 50
    max_chars = 90_000

    def __init__(self, key: str) -> None:
        if not key:
            raise EngineError("auth", "Falta la clave de DeepL. Añádela en Ajustes.")
        self.key = key
        default = "https://api-free.deepl.com" if key.endswith(":fx") else "https://api.deepl.com"
        self.base = os.environ.get("LUMBRE_DEEPL_URL", default)

    def _classify(self, response):
        if response.status_code == 456:
            return EngineError("quota", "Se ha agotado la cuota mensual de DeepL. Lo traducido queda guardado.")
        if response.status_code == 403:
            return EngineError("auth", "DeepL no acepta la clave. Revísala en Ajustes.")
        return None

    def translate(self, fragments, source, target, context=""):
        body = {
            "text": fragments,
            "target_lang": {"en": "EN-GB", "pt": "PT-PT"}.get(target, target.upper()),
            "tag_handling": "html",
            "tag_handling_version": "v2",
        }
        if source != "auto":
            body["source_lang"] = source.upper()
        if context:
            body["context"] = context
        response = _request(
            "POST", f"{self.base}/v2/translate", self._classify,
            headers={"Authorization": f"DeepL-Auth-Key {self.key}"}, json=body,
        )
        return [item["text"] for item in response.json()["translations"]]

    def remaining_quota(self):
        response = _request(
            "GET", f"{self.base}/v2/usage", self._classify,
            headers={"Authorization": f"DeepL-Auth-Key {self.key}"},
        )
        data = response.json()
        return max(0, int(data["character_limit"]) - int(data["character_count"]))


class AzureEngine(Engine):
    id = "azure"
    label = "Azure Translator"
    free_quota = 2_000_000
    max_items = 100
    max_chars = 10_000
    # El nivel gratuito limita a unos 33.300 caracteres por minuto.
    chars_per_minute = 28_000

    def __init__(self, key: str, region: str) -> None:
        if not key:
            raise EngineError("auth", "Falta la clave de Azure Translator. Añádela en Ajustes.")
        self.key = key
        self.region = region
        self.base = os.environ.get("LUMBRE_AZURE_URL", "https://api.cognitive.microsofttranslator.com")

    def _classify(self, response):
        if response.status_code == 401:
            return EngineError("auth", "Azure no acepta la clave o la región. Revísalas en Ajustes.")
        if response.status_code == 403:
            return EngineError("quota", "Se ha agotado la cuota de Azure. Lo traducido queda guardado.")
        return None

    def translate(self, fragments, source, target, context=""):
        params = {"api-version": "3.0", "to": target, "textType": "html"}
        if source != "auto":
            params["from"] = source
        headers = {"Ocp-Apim-Subscription-Key": self.key, "Content-Type": "application/json; charset=UTF-8"}
        if self.region:
            headers["Ocp-Apim-Subscription-Region"] = self.region
        response = _request(
            "POST", f"{self.base}/translate", self._classify,
            params=params, headers=headers, data=json.dumps([{"Text": f} for f in fragments]).encode("utf-8"),
        )
        return [item["translations"][0]["text"] for item in response.json()]


class LibreEngine(Engine):
    id = "libre"
    label = "LibreTranslate"
    max_items = 1

    def __init__(self, url: str, key: str = "") -> None:
        self.base = url.rstrip("/")
        self.key = key

    def _classify(self, response):
        if response.status_code in (401, 403):
            return EngineError("auth", "LibreTranslate rechaza la conexión. Revisa dirección y clave en Ajustes.")
        return None

    def translate(self, fragments, source, target, context=""):
        results = []
        for fragment in fragments:
            body = {"q": fragment, "source": source, "target": target, "format": "html"}
            if self.key:
                body["api_key"] = self.key
            response = _request("POST", f"{self.base}/translate", self._classify, json=body)
            results.append(response.json()["translatedText"])
        return results


def claude_message(key: str, model: str, prompt: str, max_tokens: int = 8000) -> str:
    def classify(response):
        if response.status_code in (401, 403):
            return EngineError("auth", "Claude no acepta la clave de la API. Revísala en Ajustes.")
        if response.status_code == 400 and "credit" in response.text.lower():
            return EngineError("quota", "La cuenta de la API de Claude no tiene saldo.")
        return None

    base = os.environ.get("LUMBRE_CLAUDE_URL", "https://api.anthropic.com")
    response = _request(
        "POST", f"{base}/v1/messages", classify,
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]},
    )
    data = response.json()
    return "".join(block.get("text", "") for block in data.get("content", []))


class ClaudeEngine(Engine):
    id = "claude"
    label = "Claude"
    max_items = 25
    max_chars = 12_000

    def __init__(self, key: str, model: str) -> None:
        if not key:
            raise EngineError("auth", "Falta la clave de la API de Claude. Añádela en Ajustes.")
        self.key = key
        self.model = model

    @property
    def memory_name(self) -> str:
        return f"claude:{self.model}"

    def translate(self, fragments, source, target, context=""):
        source_name = "the detected language" if source == "auto" else LANGUAGES.get(source, source)
        prompt = (
            f"Translate each HTML fragment of this essay from {source_name} into {LANGUAGES.get(target, target)}. "
            "Use natural, precise prose suitable for a published book. Keep every tag and attribute exactly. "
            "Do not translate the content of elements with class notranslate. Do not add commentary.\n"
            f"Text that comes just before, for context only: {context or '(none)'}\n"
            "Reply with only a JSON array of strings, one per fragment, in the same order.\n\n"
            f"Fragments: {json.dumps(fragments, ensure_ascii=False)}"
        )
        reply = claude_message(self.key, self.model, prompt, max_tokens=16000)
        match = re.search(r"\[.*\]", reply, re.S)
        try:
            items = json.loads(match.group(0)) if match else None
        except ValueError:
            items = None
        if not isinstance(items, list) or len(items) != len(fragments):
            if len(fragments) == 1:
                raise EngineError("bad", "Claude devolvió una respuesta que no se pudo leer.")
            half = len(fragments) // 2
            return (
                self.translate(fragments[:half], source, target, context)
                + self.translate(fragments[half:], source, target, context)
            )
        return [str(item) for item in items]


def build_engine(engine_id: str, config: dict) -> Engine:
    if engine_id == "prueba":
        return DemoEngine()
    if engine_id == "deepl":
        return DeepLEngine(config["deepl_key"])
    if engine_id == "azure":
        return AzureEngine(config["azure_key"], config["azure_region"])
    if engine_id == "libre":
        return LibreEngine(config["libre_url"], config["libre_key"])
    if engine_id == "claude":
        return ClaudeEngine(config["claude_key"], config["claude_model"])
    raise EngineError("bad", "Motor desconocido.")


ENGINE_INFO = [
    {"id": "azure", "label": "Azure Translator", "quota": 2_000_000, "note": "La cuota gratuita más amplia"},
    {"id": "deepl", "label": "DeepL", "quota": 500_000, "note": "Admite contexto entre párrafos"},
    {"id": "libre", "label": "LibreTranslate", "quota": 0, "note": "Instalado en vuestro equipo"},
    {"id": "claude", "label": "Claude", "quota": 0, "note": "De pago por uso"},
    {"id": "prueba", "label": "Prueba sin clave", "quota": 0, "note": "No traduce; comprueba la maquetación"},
]
