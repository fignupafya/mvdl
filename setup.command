#!/bin/bash
# mvdl one-time setup (macOS). Double-click me in Finder.
cd "$(dirname "$0")"
python3 setup.py
echo ""
read -p "Press Enter to close..."
