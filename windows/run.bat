@echo off
REM Run the bridge in a console window (Ctrl+C to stop).
cd /d "%~dp0.."
if not exist ".venv\Scripts\whoop-bridge.exe" (
    echo Not set up yet. Run: powershell -ExecutionPolicy Bypass -File windows\setup.ps1
    pause
    exit /b 1
)
".venv\Scripts\whoop-bridge.exe" run -c config.toml
pause
