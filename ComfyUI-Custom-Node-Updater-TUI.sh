#!/usr/bin/env sh
# Launcher for ComfyUI-Custom-Node-Updater-TUI on macOS and Linux.
#
# Finds a suitable Python interpreter, preferring a ComfyUI virtual environment
# without hardcoding its name, then runs the TUI script with UTF-8 mode enabled.
# Mirrors ComfyUI-Custom-Node-Updater-TUI.bat / .ps1.
#
# Interpreter resolution order:
#   1. $COMFYUI_PYTHON, if set and executable.
#   2. A venv inside $COMFYUI_DIR (default: two levels above this script),
#      looking for .venv, venv, env, .venv132, or venv132 and using bin/python.
#   3. The same venv names in the script's parent directory, then the script
#      folder.
#   4. python3, then python, on your PATH.
#
# Any arguments are forwarded to the Python script.
#
# Usage: ./ComfyUI-Custom-Node-Updater-TUI.sh [--no-fetch] [--no-autostash] ...

set -eu

# Resolve the directory this script lives in, following symlinks where possible.
script_path=$0
while [ -h "$script_path" ]; do
    link_target=$(readlink "$script_path")
    case $link_target in
        /*) script_path=$link_target ;;
        *) script_path=$(dirname "$script_path")/$link_target ;;
    esac
done
script_dir=$(CDPATH= cd -- "$(dirname -- "$script_path")" && pwd)
script_file=$script_dir/ComfyUI-Custom-Node-Updater-TUI.py

find_venv_python() {
    # $1 = directory to look in for a venv
    [ -n "${1:-}" ] || return 0
    for name in .venv venv env .venv132 venv132; do
        candidate=$1/$name/bin/python
        if [ -x "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 0
}

python=

if [ -n "${COMFYUI_PYTHON:-}" ] && [ -x "$COMFYUI_PYTHON" ]; then
    python=$COMFYUI_PYTHON
fi

comfy_dir=${COMFYUI_DIR:-$script_dir/../..}

if [ -z "$python" ]; then python=$(find_venv_python "$comfy_dir"); fi
if [ -z "$python" ]; then python=$(find_venv_python "$script_dir/.."); fi
if [ -z "$python" ]; then python=$(find_venv_python "$script_dir"); fi

if [ -z "$python" ]; then
    if command -v python3 >/dev/null 2>&1; then
        python=python3
    else
        python=python
    fi
fi

exec "$python" -X utf8 "$script_file" "$@"
