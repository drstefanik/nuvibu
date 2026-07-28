@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
  echo Errore: Python non trovato. Installa Python 3.11 o successivo.
  exit /b 1
)
where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo Errore: FFmpeg non trovato. Installalo e riprova.
  exit /b 1
)

if not exist .env copy /Y .env.example .env >nul
if not exist .venv py -3 -m venv .venv
call .venv\Scripts\activate.bat
if errorlevel 1 exit /b 1
python -m pip install --upgrade pip
if errorlevel 1 exit /b 1
python -m pip install -r requirements-dev.txt
if errorlevel 1 exit /b 1
python scripts\seed_demo.py --render
if errorlevel 1 exit /b 1
start "" http://localhost:8000
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
