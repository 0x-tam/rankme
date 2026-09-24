#!/bin/bash
set -u
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$SCRIPT_DIR/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  echo "RankMe needs a local Python environment. In Terminal, run:"
  echo "  cd \"$SCRIPT_DIR\""
  echo "  python3.12 -m venv .venv"
  echo "  .venv/bin/python -m pip install -r requirements.txt"
  read -r -p "Press Return to close. "
  exit 1
fi
"$PYTHON" "$SCRIPT_DIR/scripts/launch.py"
RESULT=$?
if [ "$RESULT" -ne 0 ]; then
  read -r -p "Press Return to close. "
fi
exit "$RESULT"
