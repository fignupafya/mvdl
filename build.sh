#!/bin/bash
# Build a standalone mvdl binary (Linux). Needs: python3 -m pip install pyinstaller
cd "$(dirname "$0")"
python3 build.py
