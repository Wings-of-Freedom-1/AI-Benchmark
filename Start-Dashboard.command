#!/usr/bin/env bash
# Double-click launcher for macOS (.command copy) and Linux. First run creates a private venv.
cd "$(dirname "$0")" || exit 1
PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then echo "Python 3.10+ is required: https://www.python.org/downloads/"; read -r -p "Press Enter to close"; exit 1; fi
if [ ! -x .venv/bin/python ]; then
  echo "First run: setting things up (about a minute, needs internet)..."
  "$PY" -m venv .venv && .venv/bin/python -m pip install --quiet --upgrade pip && .venv/bin/python -m pip install --quiet -r requirements.txt \
    || { echo "Setup failed."; read -r -p "Press Enter to close"; exit 1; }
fi
PYTHONPATH="$PWD/src" exec .venv/bin/python -m aimodels dashboard
