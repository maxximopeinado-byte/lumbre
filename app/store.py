"""Carpetas de datos, configuración local y memoria de traducciones."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _default_data() -> Path:
    if os.environ.get("LUMBRE_DATOS"):
        return Path(os.environ["LUMBRE_DATOS"])
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "Lumbre" / "datos"
    return ROOT / "datos"


DATA = _default_data()
JOBS = DATA / "trabajos"
CONFIG_PATH = DATA / "ajustes.json"

DEFAULT_CONFIG = {
    "deepl_key": "",
    "azure_key": "",
    "azure_region": "westeurope",
    "libre_url": "http://localhost:5000",
    "libre_key": "",
    "claude_key": "",
    "claude_model": "claude-sonnet-5-5",
    "claude_summary_model": "claude-haiku-5-5",
    "glosario_general": "",
    "ia_motor": "",
    "ia_groq_clave": "",
    "ia_groq_modelo": "",
    "ia_mistral_clave": "",
    "ia_mistral_modelo": "",
    "ia_ollama_modelo": "",
    "ia_ollama_url": "",
    "ia_personalizada_clave": "",
    "ia_personalizada_url": "",
    "ia_personalizada_modelo": "",
}
SECRET_FIELDS = ("deepl_key", "azure_key", "libre_key", "claude_key", "ia_groq_clave", "ia_mistral_clave", "ia_personalizada_clave")


def ensure_dirs() -> None:
    JOBS.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    ensure_dirs()
    config = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            config.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    return config


def save_config(changes: dict) -> dict:
    config = load_config()
    for key, value in changes.items():
        if key in DEFAULT_CONFIG and isinstance(value, str):
            config[key] = value.strip()
    CONFIG_PATH.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    return config


def public_config(config: dict) -> dict:
    """Nunca devuelve una clave completa a la pantalla."""
    result = {}
    for key, value in config.items():
        if key in SECRET_FIELDS:
            result[key] = (f"…{value[-4:]}" if len(value) > 8 else "guardada") if value else ""
        else:
            result[key] = value
    return result


class Memory:
    """Guarda cada fragmento traducido para reanudar sin gastar cuota de nuevo."""

    def __init__(self, path: Path | None = None) -> None:
        ensure_dirs()
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path or DATA / "memoria.sqlite", check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS tr (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS uso (motor TEXT, mes TEXT, caracteres INTEGER, PRIMARY KEY (motor, mes))"
        )
        self._db.commit()

    @staticmethod
    def key(engine: str, source: str, target: str, fragment: str) -> str:
        raw = "\x1f".join((engine, source, target, fragment)).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def get(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT v FROM tr WHERE k = ?", (key,)).fetchone()
        return row[0] if row else None

    def put_many(self, pairs: list[tuple[str, str]]) -> None:
        with self._lock:
            self._db.executemany("INSERT OR REPLACE INTO tr (k, v) VALUES (?, ?)", pairs)
            self._db.commit()

    def add_usage(self, engine: str, characters: int) -> None:
        month = date.today().strftime("%Y-%m")
        with self._lock:
            self._db.execute(
                "INSERT INTO uso (motor, mes, caracteres) VALUES (?, ?, ?) "
                "ON CONFLICT(motor, mes) DO UPDATE SET caracteres = caracteres + excluded.caracteres",
                (engine, month, characters),
            )
            self._db.commit()

    def usage(self, engine: str) -> int:
        month = date.today().strftime("%Y-%m")
        with self._lock:
            row = self._db.execute(
                "SELECT caracteres FROM uso WHERE motor = ? AND mes = ?", (engine, month)
            ).fetchone()
        return row[0] if row else 0
