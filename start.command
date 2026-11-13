#!/bin/sh
# Doppelklick (macOS/Linux): startet das Datierungstool.
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 fehlt. Bitte von https://www.python.org/downloads/ installieren."
  read -r _
  exit 1
fi
exec python3 python/date_tool.py "$@"
