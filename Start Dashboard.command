#!/usr/bin/env bash
# Opens the stock planner dashboard. First run sets itself up (needs internet).
cd "$(dirname "$0")" || exit 1
PY=$(command -v python3 || command -v python)
if [ -z "$PY" ]; then
  echo "Python is not installed. Install Python 3 from https://www.python.org/downloads/ and run this again."
  read -r -p "Press Enter to close..."; exit 1
fi
if [ ! -x .venv/bin/python ]; then
  echo "Setting up for the first time. Please wait..."
  "$PY" -m venv .venv && .venv/bin/python -m pip install --quiet -r requirements.txt || {
    echo "Setup did not finish. Check your internet connection and try again."
    rm -rf .venv; read -r -p "Press Enter to close..."; exit 1; }
fi
echo
echo "Opening the dashboard in your web browser. Leave this window open; press Ctrl+C to stop."
.venv/bin/python dashboard.py "$@"
