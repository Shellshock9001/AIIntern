# ARGUS FinDash - one-command launcher (PowerShell)
# Usage:  .\run.ps1   |   .\run.ps1 eval   |   .\run.ps1 ingest   |   .\run.ps1 doctor
# If script execution is blocked, run once:
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

$ErrorActionPreference = "Stop"

# --- Find a Python launcher: py -> python -> python3 ---
$pyexe = $null
foreach ($cand in @("py", "python", "python3")) {
    if (Get-Command $cand -ErrorAction SilentlyContinue) { $pyexe = $cand; break }
}
if (-not $pyexe) {
    Write-Host ""
    Write-Host "Python was not found on your PATH." -ForegroundColor Red
    Write-Host "Install Python 3.10+ from https://www.python.org/downloads/"
    Write-Host 'and tick "Add python.exe to PATH" during setup, then re-run .\run.ps1'
    exit 1
}

# --- Create venv on first run ---
if (-not (Test-Path ".venv\Scripts\Activate.ps1")) {
    Write-Host "Creating virtual environment..."
    & $pyexe -m venv .venv
}

& ".venv\Scripts\Activate.ps1"

# --- Install deps only if Streamlit isn't importable yet ---
python -c "import streamlit" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing dependencies, one time..."
    python -m pip install --upgrade pip | Out-Null
    pip install -r requirements.txt
}

python run.py @args
