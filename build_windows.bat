@echo off
setlocal
cd /d "%~dp0"
python -m pip install pyinstaller --target .deps
set PYTHONPATH=.deps
python -m PyInstaller --noconfirm --onedir --windowed --name TianjinCertQuery --add-data ".playwright-browsers;.playwright-browsers" --add-data ".deps;.deps" query_cert_gui.py
echo.
echo Build finished. Run dist\TianjinCertQuery\TianjinCertQuery.exe
pause
