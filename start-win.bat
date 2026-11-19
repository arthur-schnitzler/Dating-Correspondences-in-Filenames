@echo off
rem Doppelklick (Windows): startet das Datierungstool.
cd /d "%~dp0"
where py >/dev/null 2>/dev/null && (py -3 python\date_tool.py %* & goto :eof)
where python >/dev/null 2>/dev/null && (python python\date_tool.py %* & goto :eof)
echo Python 3 fehlt. Bitte von https://www.python.org/downloads/ installieren.
pause
