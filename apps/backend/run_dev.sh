#!/usr/bin/env bash
# Dev launcher: pins cwd to apps/backend so the SQLite DB path resolves correctly.
cd "$(dirname "$0")"
exec ./.venv/Scripts/python.exe -m uvicorn opspilot_backend.main:app --host 127.0.0.1 --port "$1" --log-level info
