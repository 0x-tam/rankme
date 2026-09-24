#!/bin/bash
set -u
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3.9 or later is required. Install Python, then open RankMe again."
  read -r -p "Press Return to close. "
  exit 1
fi
python3 "$SCRIPT_DIR/scripts/launch.py"
RESULT=$?
if [ "$RESULT" -ne 0 ]; then
  read -r -p "Press Return to close. "
fi
exit "$RESULT"
