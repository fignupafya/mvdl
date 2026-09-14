#!/usr/bin/env python3
"""Build a standalone mvdl app (no Python needed to run the result).

Requires PyInstaller (`py -m pip install pyinstaller`). Produces:
  Windows : dist/mvdl/mvdl.exe   (folder app, no console)
  macOS   : dist/mvdl.app
  Linux   : dist/mvdl/mvdl

The result still uses the system browser (Edge/Chrome) for auto-detection; if a
machine has none, run `playwright install chromium` once on it.
"""
import os
import subprocess
import sys

SEP = ";" if os.name == "nt" else ":"


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    os.chdir(here)
    args = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--name", "mvdl", "--onedir", "--windowed",
        "--add-data", f"web{SEP}web",
        "--collect-all", "playwright",
        "--collect-all", "imageio_ffmpeg",
        "--hidden-import", "engine",
        "--hidden-import", "detector",
        "--hidden-import", "hlsdl",
        "app.py",
    ]
    print(">", " ".join(args), flush=True)
    try:
        subprocess.run(args, check=True)
    except FileNotFoundError:
        print("PyInstaller not installed. Run: py -m pip install pyinstaller")
        return 1
    except subprocess.CalledProcessError as e:
        print("Build failed:", e)
        return 1
    out = os.path.join(here, "dist")
    print(f"\nBuild done -> {out}")
    if sys.platform.startswith("win"):
        print("Run: dist\\mvdl\\mvdl.exe")
    elif sys.platform == "darwin":
        print("Run: open dist/mvdl.app")
    else:
        print("Run: dist/mvdl/mvdl")
    return 0


if __name__ == "__main__":
    sys.exit(main())
