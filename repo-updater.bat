@echo off
setlocal

cd /d "%~dp0"

set "PY=%~dp0..\ComfyUI\.venv132\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" -X utf8 "%~dp0repo_updater_tui.py" %*
pause