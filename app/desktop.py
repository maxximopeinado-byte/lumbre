"""Modo escritorio: una sola ventana, sin consola, y un único programa en marcha."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

import uvicorn

from . import __version__, migrate, server, store

IDLE_LIMIT = 150
FIRST_WINDOW_LIMIT = 180


def _info_path() -> Path:
    return store.DATA.parent / "servidor.json"


def _running_url() -> str | None:
    try:
        info = json.loads(_info_path().read_text(encoding="utf-8"))
        url = f"http://127.0.0.1:{int(info['puerto'])}"
        with urllib.request.urlopen(f"{url}/api/estado", timeout=2) as response:
            return url if response.status == 200 else None
    except (OSError, ValueError, KeyError):
        return None


def _free_port(start: int = 8765) -> int:
    for port in range(start, start + 30):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit("No hay ningún puerto libre.")


def _wait_for_port(port: int) -> None:
    for _ in range(60):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
                return
            except OSError:
                time.sleep(0.25)


def _browser() -> str | None:
    roots = [os.environ.get(name, "") for name in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
    tails = [("Microsoft", "Edge", "Application", "msedge.exe"), ("Google", "Chrome", "Application", "chrome.exe")]
    for tail in tails:
        for root in roots:
            candidate = Path(root, *tail) if root else None
            if candidate and candidate.exists():
                return str(candidate)
    return None


def open_window(url: str) -> None:
    browser = _browser() if os.name == "nt" else None
    if not browser:
        webbrowser.open(url)
        return
    profile = store.DATA.parent / "ventana"
    subprocess.Popen(
        [
            browser, f"--app={url}", f"--user-data-dir={profile}", "--window-size=1180,820", "--no-first-run",
            "--no-default-browser-check", "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
        ],
        creationflags=0x08000000, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _watch(uv: uvicorn.Server) -> None:
    """Se cierra cuando ya no hay ventana abierta ni traducción en marcha."""
    started = time.monotonic()
    while not uv.should_exit:
        time.sleep(5)
        last = server.life["latido"] if server.life["visto"] else started
        limit = IDLE_LIMIT if server.life["visto"] else FIRST_WINDOW_LIMIT
        if time.monotonic() - last > limit and not server.is_busy():
            uv.should_exit = True


def main(arguments: list[str]) -> None:
    # MuPDF necesita más pila de la que Windows da por defecto a cada hilo en páginas escaneadas.
    threading.stack_size(32 * 1024 * 1024)
    store.ensure_dirs()
    quiet = "--sin-ventana" in arguments
    if not quiet:
        url = _running_url()
        if url:
            open_window(url)
            return
    if "--puerto" in arguments:
        port = int(arguments[arguments.index("--puerto") + 1])
        _wait_for_port(port)
    else:
        port = _free_port()
    os.environ["LUMBRE_PUERTO"] = str(port)
    _info_path().write_text(json.dumps({"puerto": port, "pid": os.getpid(), "version": __version__}), encoding="utf-8")

    server.data_ready.clear()

    def prepare() -> None:
        migrate.run()
        server.recover_jobs()
        server.data_ready.set()

    threading.Thread(target=prepare, daemon=True).start()
    uv = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=port, log_config=None, access_log=False))
    threading.Thread(target=_watch, args=(uv,), daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    if not quiet:
        threading.Timer(0.7, open_window, args=(url,)).start()
    print(f"Lumbre {__version__} en {url}", flush=True)
    uv.run()
