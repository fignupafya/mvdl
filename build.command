#!/bin/bash
# Build a standalone mvdl.app (macOS). Needs: python3 -m pip install pyinstaller
cd "$(dirname "$0")"
python3 build.py
echo ""
read -p "Press Enter to close..."
