#!/bin/bash
# Launches the web UI with settings from .env.local (if present).
# Used directly, or as the command a launchd service runs to keep this
# running in the background across restarts/crashes.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

if [ -f .env.local ]; then
  set -a
  source .env.local
  set +a
fi

exec .venv/bin/python app.py
