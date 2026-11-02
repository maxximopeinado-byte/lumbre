"""Actualiza solo el código de la app, sin reinstalar ni tocar los datos."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import requests

from . import store

REPOSITORY = "maxximopeinado-byte/lumbre"
REQUIRED = {"app/__init__.py", "app/server.py", "app/desktop.py", "app/static/index.html"}


class UpdateError(Exception):
    pass


def folder() -> Path:
    return store.DATA.parent / "actualizaciones"


def _parts(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError as exc:
        raise UpdateError("La actualización tiene una versión no válida.") from exc


def install(path: Path, current: str) -> str:
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise UpdateError("Ese archivo no es una actualización de Lumbre.") from exc
    with archive:
        names = [name for name in archive.namelist() if not name.endswith("/")]
        if "actualizacion.json" not in names or not REQUIRED.issubset(names):
            raise UpdateError("Ese archivo no es una actualización de Lumbre.")
        for name in names:
            pure = Path(name)
            if name != "actualizacion.json" and (pure.parts[0] != "app" or ".." in pure.parts or pure.is_absolute() or "\\" in name):
                raise UpdateError("La actualización contiene rutas no permitidas.")
        version = str(json.loads(archive.read("actualizacion.json")).get("version", ""))
        if _parts(version) <= _parts(current):
            raise UpdateError(f"Ya tienes la versión {current} o una posterior.")
        root = folder()
        root.mkdir(parents=True, exist_ok=True)
        target = root / version
        pending = root / f"{version}.nueva"
        shutil.rmtree(pending, ignore_errors=True)
        for name in names:
            if name == "actualizacion.json":
                continue
            destination = pending / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive.read(name))
    shutil.rmtree(target, ignore_errors=True)
    pending.replace(target)
    (root / "actual.json").write_text(json.dumps({"version": version}), encoding="utf-8")
    return version


def latest() -> dict | None:
    """Consulta la última versión publicada. Devuelve None si no hay conexión."""
    try:
        response = requests.get(f"https://api.github.com/repos/{REPOSITORY}/releases/latest", timeout=8, headers={"Accept": "application/vnd.github+json"})
        if response.status_code != 200:
            return None
        data = response.json()
    except (requests.RequestException, ValueError):
        return None
    version = str(data.get("tag_name", "")).lstrip("v")
    asset = next((a for a in data.get("assets", []) if a["name"].startswith("Lumbre-actualizacion-")), None)
    installer = next((a for a in data.get("assets", []) if a["name"].startswith("Lumbre-instalador-")), None)
    if not version or not asset:
        return None
    return {"version": version, "url": asset["browser_download_url"], "notas": data.get("body") or "", "instalador": installer["browser_download_url"] if installer else ""}


def is_newer(version: str, current: str) -> bool:
    try:
        return _parts(version) > _parts(current)
    except UpdateError:
        return False


def download(url: str, destination: Path) -> None:
    if not url.startswith(f"https://github.com/{REPOSITORY}/"):
        raise UpdateError("La dirección de la actualización no es la esperada.")
    try:
        with requests.get(url, timeout=60, stream=True) as response:
            response.raise_for_status()
            with destination.open("wb") as out:
                for chunk in response.iter_content(1024 * 256):
                    out.write(chunk)
    except requests.RequestException as exc:
        raise UpdateError("No se ha podido descargar la actualización.") from exc


def schedule_restart() -> bool:
    """Arranca el programa actualizado sin abrir otra ventana y cierra este."""
    launcher = os.environ.get("LUMBRE_ARRANQUE")
    port = os.environ.get("LUMBRE_PUERTO")
    if not launcher or not port:
        return False

    def restart() -> None:
        time.sleep(0.6)
        flags = 0x00000008 | 0x00000200 if os.name == "nt" else 0  # Sin consola y en su propio grupo.
        subprocess.Popen(
            [sys.executable, launcher, "--sin-ventana", "--puerto", port],
            creationflags=flags, close_fds=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        os._exit(0)

    threading.Thread(target=restart, daemon=True).start()
    return True
