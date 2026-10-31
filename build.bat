@echo off
rem Builds dist\TubeLoader.exe - a single file with Python and the UI bundled in.
rem Needs Python 3.9+ on the BUILD machine only; the finished exe needs nothing.
cd /d "%~dp0"
python -m pip install --quiet --upgrade pyinstaller || exit /b 1
python -m PyInstaller --noconfirm --clean --onefile --noconsole --name TubeLoader --icon icon.ico ^
  --add-data "ui;ui" TubeLoader.pyw || exit /b 1
echo.
echo Built: %~dp0dist\TubeLoader.exe
