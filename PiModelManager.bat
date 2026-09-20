@echo off
rem Pi Model Manager launcher
rem Always run from this script's own folder.
cd /d "%~dp0"

where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "%~dp0desktop_app.py"
    exit /b 0
)

where python >nul 2>nul
if %errorlevel%==0 (
    start "" python "%~dp0desktop_app.py"
    exit /b 0
)

echo [ERROR] Python not found in PATH.
pause
