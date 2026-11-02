"""Arranca Lumbre para desarrollo o desde la carpeta de código."""
import sys

from .desktop import main

if __name__ == "__main__":
    main(sys.argv[1:])
