#!/usr/bin/env python3
"""mvdl setup - installs Python deps (Playwright, ffmpeg) and a browser for detection.

Run once on a fresh machine:
  Windows : double-click setup.bat   (or: py setup.py)
  macOS   : double-click setup.command
  Linux   : ./setup.sh               (or: python3 setup.py)
"""
import subprocess
import sys


def run(args):
    print(">", " ".join(args), flush=True)
    subprocess.run(args, check=True)


def main():
    print("mvdl setup - Python", sys.version.split()[0], "on", sys.platform)
    if sys.version_info < (3, 8):
        print("ERROR: Python 3.8+ is required.")
        return 1
    py = sys.executable
    try:
        run([py, "-m", "pip", "install", "--upgrade", "pip"])
        run([py, "-m", "pip", "install", "--upgrade", "playwright", "imageio-ffmpeg"])
        # Bundled Chromium so auto-detection works even without system Chrome/Edge.
        # On Linux also pull the system libraries Chromium needs (uses sudo/apt).
        if sys.platform.startswith("linux"):
            run([py, "-m", "playwright", "install", "--with-deps", "chromium"])
        else:
            run([py, "-m", "playwright", "install", "chromium"])
    except subprocess.CalledProcessError as e:
        print("\nSetup FAILED:", e)
        print("Check your internet connection and try again.")
        return 1

    print("\nSetup complete. To start mvdl:")
    if sys.platform.startswith("win"):
        print("  double-click  mvdl.vbs        (no console window)")
        print("  or debug with baslat.bat / run: python app.py")
    elif sys.platform == "darwin":
        print("  double-click  mvdl.command    (or run: python3 app.py)")
    else:
        print("  run  ./mvdl.sh                (or: python3 app.py)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
