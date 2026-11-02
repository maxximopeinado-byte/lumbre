#!/bin/bash
# Genera el instalador para Windows y el archivo de actualización. Se ejecuta en Linux.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; ROOT="$(dirname "$HERE")"
TOOLS="${1:?carpeta de herramientas}"; OUT="${2:?carpeta de salida}"
VERSION="$(sed -n 's/__version__ = "\(.*\)"/\1/p' "$ROOT/app/__init__.py")"
PIP="$TOOLS/venv/bin/pip"; BUILD="$TOOLS/win/paquete"; RT="$TOOLS/win/rt"
rm -rf "$BUILD"; mkdir -p "$BUILD/codigo" "$OUT"

if [ "${SOLO_ACTUALIZACION:-}" != "1" ]; then
  cp -a "$RT" "$BUILD/runtime"
  cd "$BUILD/runtime"
  rm -rf Tools include conda-meta share Scripts Library/include Library/lib/*.lib Library/lib/tcl* Library/lib/tk* Library/mingw-w64 tcl
  rm -rf Lib/test Lib/idlelib Lib/tkinter Lib/turtledemo Lib/ensurepip Lib/lib2to3 Lib/pydoc_data Lib/site-packages/pip* Lib/site-packages/setuptools* Lib/site-packages/wheel*
  rm -f Library/bin/tcl*.dll Library/bin/tk*.dll DLLs/_tkinter.pyd Library/bin/*.exe
  find . -name __pycache__ -type d -prune -exec rm -rf {} +
  find . -name '*.pdb' -delete; find . -name '*.lib' -delete; find . -name '*.h' -delete
  rm -rf "$TOOLS/win/ruedas"; mkdir -p "$TOOLS/win/ruedas"
  "$PIP" download -q -r "$ROOT/requirements.txt" colorama --only-binary=:all: --platform win_amd64 --python-version 3.13 --implementation cp -d "$TOOLS/win/ruedas"
  for w in "$TOOLS"/win/ruedas/*.whl; do python3 -m zipfile -e "$w" Lib/site-packages/; done
  rm -rf Lib/site-packages/*/tests Lib/site-packages/pymupdf/mupdf-devel Lib/site-packages/fitz_old
  find Lib/site-packages -name __pycache__ -type d -prune -exec rm -rf {} +
  find Lib/site-packages -name '*.lib' -delete; find Lib/site-packages -name '*.pdb' -delete; find Lib/site-packages -name '*.h' -delete
  cd "$HERE"
  x86_64-w64-mingw32-windres lumbre.rc -O coff -o "$TOOLS/win/lumbre.res"
  x86_64-w64-mingw32-gcc -municode -mwindows -Os -s lumbre.c "$TOOLS/win/lumbre.res" -o "$BUILD/Lumbre.exe"
  cp arranque.py lumbre.ico "$BUILD/"
  cp -a "$ROOT/app" "$BUILD/codigo/app"
  find "$BUILD/codigo" -name __pycache__ -type d -prune -exec rm -rf {} +
  makensis -V2 -DVERSION="$VERSION" -DORIGEN="$BUILD" -DSALIDA="$OUT/Lumbre-instalador-$VERSION.exe" "$HERE/instalador.nsi"
fi

python3 - "$ROOT" "$OUT" "$VERSION" <<'PY'
import json, os, sys, zipfile
root, out, version = sys.argv[1:]
with zipfile.ZipFile(f"{out}/Lumbre-actualizacion-{version}.zip", "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("actualizacion.json", json.dumps({"version": version}))
    for base, dirs, files in os.walk(f"{root}/app"):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            path = os.path.join(base, name)
            z.write(path, os.path.relpath(path, root).replace(os.sep, "/"))
PY
ls -la "$OUT"
