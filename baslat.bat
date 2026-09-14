@echo off
REM mvdl - debug launcher (keeps a console so you can see errors).
REM Normal use: double-click mvdl.vbs (no console window).
cd /d "%~dp0"
py app.py
echo.
echo (mvdl kapandi / closed. Hata varsa yukarida gorunur.)
pause
