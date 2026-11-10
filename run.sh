#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/python -m pip install -q -r requirements.txt
.venv/bin/python -m playwright install chromium
exec .venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port "${MUSE_PORT:-8765}"
