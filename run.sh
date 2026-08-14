#!/usr/bin/env bash
# One-command launcher for the Usage Cost Monitor.
# First run: creates .venv (if missing) + installs requirements, then starts
# Streamlit. Later runs just start the app. Always uses THIS folder's .venv,
# so it works no matter where you invoke it from.
set -e
cd "$(dirname "$0")"

# Prefer a python that can host the deps; fall back to python3.
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  for c in /usr/local/bin/python3.11 /usr/local/bin/python3.12 python3; do
    command -v "$c" >/dev/null 2>&1 && { PY="$c"; break; }
  done
fi
[ -n "$PY" ] || { echo "python3 not found"; exit 1; }

if [ ! -d .venv ]; then
  echo "[run] creating venv with $PY ..."
  "$PY" -m venv .venv
fi

if ! .venv/bin/python -c "import streamlit, plotly" >/dev/null 2>&1; then
  echo "[run] installing requirements ..."
  .venv/bin/pip install -q -r requirements.txt
fi

echo "[run] starting Streamlit ..."
exec .venv/bin/streamlit run app.py "$@"
