"""Recupera libros, claves y memoria de la versión anterior, que vivía en su carpeta."""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from . import store

result: dict = {}
SKIP = {"appdata", "node_modules", ".venv", "$recycle.bin", "windows", "program files", "program files (x86)"}


def _find(limit_seconds: float = 12) -> Path | None:
    home = Path.home()
    roots = [home / "Downloads", home / "Desktop", home / "Documents", home / "OneDrive", home]
    deadline = time.monotonic() + limit_seconds
    found: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        base_depth = len(root.parts)
        for current, directories, _ in os.walk(root):
            if time.monotonic() > deadline:
                break
            path = Path(current)
            depth = len(path.parts) - base_depth
            directories[:] = [d for d in directories if d.lower() not in SKIP and not d.startswith(".")] if depth < 4 else []
            if path in seen:
                directories[:] = []
                continue
            seen.add(path)
            if path.name == "datos" and (path / "memoria.sqlite").exists() and path.resolve() != store.DATA.resolve():
                found.append(path)
                directories[:] = []
    return max(found, key=lambda p: (p / "memoria.sqlite").stat().st_mtime) if found else None


def run() -> None:
    marker = store.DATA / "recuperado.txt"
    store.ensure_dirs()
    if marker.exists():
        return
    try:
        source = _find()
        count = 0
        if source:
            for name in ("ajustes.json", "memoria.sqlite"):
                if (source / name).exists() and not (store.DATA / name).exists():
                    shutil.copy2(source / name, store.DATA / name)
            for job in (source / "trabajos").iterdir() if (source / "trabajos").exists() else []:
                destination = store.JOBS / job.name
                if job.is_dir() and not destination.exists():
                    shutil.copytree(job, destination)
                    count += 1
        result["libros"] = count
        marker.write_text(str(source or ""), encoding="utf-8")
    except OSError as exc:
        print(f"No se pudieron recuperar los datos anteriores: {exc}", flush=True)
