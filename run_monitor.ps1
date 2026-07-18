param(
    [switch]$Once,
    [switch]$Json,
    [switch]$NoTop
)

$ErrorActionPreference = "Stop"

$PythonExe = $null

if ($env:AI_USAGE_MONITOR_PYTHON -and (Test-Path -LiteralPath $env:AI_USAGE_MONITOR_PYTHON)) {
    $PythonExe = $env:AI_USAGE_MONITOR_PYTHON
}

$LocalVenvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not $PythonExe -and (Test-Path -LiteralPath $LocalVenvPython)) {
    $PythonExe = $LocalVenvPython
}

$CodexRuntimePython = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if (-not $PythonExe -and (Test-Path -LiteralPath $CodexRuntimePython)) {
    $PythonExe = $CodexRuntimePython
}

try {
    $versionOutput = & py -3 --version 2>&1
    if ($LASTEXITCODE -eq 0 -and "$versionOutput" -match "Python 3") {
        $PythonExe = "py"
        $ArgsPrefix = @("-3")
    }
} catch {
    if (-not $PythonExe) {
        $PythonExe = $null
    }
}

if (-not $PythonExe) {
    try {
        $versionOutput = & python --version 2>&1
        if ($LASTEXITCODE -eq 0 -and "$versionOutput" -match "Python 3") {
            $PythonExe = "python"
            $ArgsPrefix = @()
        }
    } catch {
        $PythonExe = $null
    }
}

if (-not $PythonExe) {
    throw "Python 3 was not found. Install Python 3, create .venv, or set AI_USAGE_MONITOR_PYTHON to a valid python.exe path."
}

$ArgsList = @()
if ($ArgsPrefix) { $ArgsList += $ArgsPrefix }
$ArgsList += "app.py"
if ($Once) { $ArgsList += "--once" }
if ($Json) { $ArgsList += "--json" }
if ($NoTop) { $ArgsList += "--no-topmost" }

$env:PYTHONDONTWRITEBYTECODE = "1"
& $PythonExe @ArgsList
