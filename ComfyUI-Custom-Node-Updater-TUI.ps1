#Requires -Version 5.1
<#
.SYNOPSIS
    Launcher for ComfyUI-Custom-Node-Updater-TUI on Windows (PowerShell).

.DESCRIPTION
    Finds a suitable Python interpreter, preferring a ComfyUI virtual
    environment without hardcoding its name, then runs the TUI script with
    UTF-8 mode enabled. Mirrors ComfyUI-Custom-Node-Updater-TUI.bat.

    Interpreter resolution order:
      1. $env:COMFYUI_PYTHON, if set and the file exists.
      2. A venv inside $env:COMFYUI_DIR (default: two levels above this script),
         looking for .venv, venv, env, .venv132, or venv132 and using
         Scripts\python.exe.
      3. The same venv names in the script's parent directory, then the script
         folder.
      4. python on your PATH.

    Any arguments are forwarded to the Python script.

.EXAMPLE
    .\ComfyUI-Custom-Node-Updater-TUI.ps1 --no-fetch
#>
[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = 'Stop'

$scriptDir = $PSScriptRoot
$scriptPath = Join-Path $scriptDir 'ComfyUI-Custom-Node-Updater-TUI.py'

function Find-VenvPython {
    param([string]$Dir)
    if ([string]::IsNullOrEmpty($Dir)) { return $null }
    foreach ($name in @('.venv', 'venv', 'env', '.venv132', 'venv132')) {
        $candidate = Join-Path $Dir (Join-Path $name 'Scripts\python.exe')
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return $candidate
        }
    }
    return $null
}

$python = $null

if ($env:COMFYUI_PYTHON -and (Test-Path -LiteralPath $env:COMFYUI_PYTHON -PathType Leaf)) {
    $python = $env:COMFYUI_PYTHON
}

$comfyDir = $env:COMFYUI_DIR
if ([string]::IsNullOrEmpty($comfyDir)) {
    $comfyDir = Join-Path $scriptDir '..\..'
}

if (-not $python) { $python = Find-VenvPython $comfyDir }
if (-not $python) { $python = Find-VenvPython (Join-Path $scriptDir '..') }
if (-not $python) { $python = Find-VenvPython $scriptDir }

if (-not $python) { $python = 'python' }

& $python -X utf8 $scriptPath @Rest
exit $LASTEXITCODE
