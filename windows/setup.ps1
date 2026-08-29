# One-time setup for whoop-bridge on Windows.
# Run from the repo root in PowerShell:  .\windows\setup.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

Write-Host "Setting up whoop-bridge in $root" -ForegroundColor Cyan

$py = Get-Command py -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command python -ErrorAction SilentlyContinue }
if (-not $py) { throw "Python not found. Install Python 3.11+ from python.org and re-run." }

$ver = & $py.Source -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ([version]$ver -lt [version]"3.11") { throw "Python $ver found, but 3.11+ is required." }
Write-Host "Python $ver OK"

if (-not (Test-Path "$root\.venv")) {
    & $py.Source -m venv "$root\.venv"
    Write-Host "Created virtualenv"
}
& "$root\.venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
& "$root\.venv\Scripts\python.exe" -m pip install -e "$root" --quiet
Write-Host "Dependencies installed"

if (-not (Test-Path "$root\config.toml")) {
    Copy-Item "$root\config.example.toml" "$root\config.toml"
    Write-Host "Created config.toml -- edit it before running" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Next:" -ForegroundColor Green
Write-Host "  1. .\.venv\Scripts\whoop-bridge.exe scan        # find your strap's address"
Write-Host "  2. Put that address in config.toml, plus your forward_url"
Write-Host "  3. .\.venv\Scripts\whoop-bridge.exe test-endpoint"
Write-Host "  4. .\.venv\Scripts\whoop-bridge.exe run"
Write-Host "  5. .\windows\install-task.ps1                   # run automatically at logon"
