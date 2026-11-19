#!/bin/sh
# Doppelklick (macOS/Linux): startet das Datierungstool.
cd "$(dirname "$0")" || exit 1
# macOS: die Download-Markierung entfernen, damit es künftig ohne Nachfrage startet.
if [ "$(uname)" = Darwin ]; then xattr -dr com.apple.quarantine . 2>/dev/null; fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 fehlt. Bitte von https://www.python.org/downloads/ installieren."
  read -r _
  exit 1
fi
exec python3 python/date_tool.py "$@"
