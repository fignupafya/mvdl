@echo off
REM Build a standalone mvdl.exe (Windows). Needs: py -m pip install pyinstaller
cd /d "%~dp0"
py build.py
echo.
pause
