"""Punto de entrada del programa instalado. No cambia con las actualizaciones."""
import json
import os
import subprocess
import sys
import traceback
from pathlib import Path

BASE = Path(__file__).resolve().parent
LOCAL = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Lumbre"
LOCAL.mkdir(parents=True, exist_ok=True)
log = open(LOCAL / "lumbre.log", "a", encoding="utf-8", buffering=1)
sys.stdout = log
sys.stderr = log
os.environ["LUMBRE_ARRANQUE"] = str(Path(__file__).resolve())
sys.path.insert(0, str(BASE / "codigo"))
pointer = LOCAL / "actualizaciones" / "actual.json"
updated = None
try:
    version = json.loads(pointer.read_text(encoding="utf-8"))["version"]
    candidate = LOCAL / "actualizaciones" / version
    if (candidate / "app" / "__init__.py").exists():
        updated = candidate
        sys.path.insert(0, str(candidate))
except (OSError, ValueError, KeyError):
    pass

try:
    from app.desktop import main
except Exception:
    traceback.print_exc()
    if updated is None:
        raise
    # Una actualización defectuosa nunca debe impedir abrir la app.
    pointer.unlink(missing_ok=True)
    subprocess.Popen([sys.executable, __file__, *sys.argv[1:]], creationflags=0x08000000 if os.name == "nt" else 0)
    raise SystemExit(0)

main(sys.argv[1:])
