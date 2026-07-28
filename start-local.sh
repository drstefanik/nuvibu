#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Errore: Python 3 non trovato. Installa Python 3.11 o successivo." >&2
  exit 1
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "Errore: FFmpeg non trovato. Installalo e riprova." >&2
  exit 1
fi

[ -f .env ] || cp .env.example .env
[ -d .venv ] || python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
python scripts/seed_demo.py --render
exec python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
