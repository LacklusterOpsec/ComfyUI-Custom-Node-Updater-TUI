@echo off
setlocal

cd /d "%~dp0"

rem Find a Python interpreter, preferring the ComfyUI venv without hardcoding
rem its name. Override with COMFYUI_PYTHON, or point COMFYUI_DIR at the ComfyUI
rem install if it is not two levels above this script.
set "PY="

if defined COMFYUI_PYTHON (
    if exist "%COMFYUI_PYTHON%" set "PY=%COMFYUI_PYTHON%"
)

if not defined COMFYUI_DIR set "COMFYUI_DIR=%~dp0..\.."
if not defined PY call :find_venv "%COMFYUI_DIR%"
if not defined PY call :find_venv "%~dp0.."
if not defined PY call :find_venv "%~dp0"

if not defined PY set "PY=python"

"%PY%" -X utf8 "%~dp0ComfyUI-Custom-Node-Updater-TUI.py" %*
set "CODE=%ERRORLEVEL%"
pause
exit /b %CODE%

:find_venv
rem %1 = directory to look in for a venv
for %%V in (".venv" "venv" "env" ".venv132" "venv132") do (
    if not defined PY if exist "%~1\%%~V\Scripts\python.exe" set "PY=%~1\%%~V\Scripts\python.exe"
)
exit /b
