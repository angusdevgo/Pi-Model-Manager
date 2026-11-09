@echo off
rem Pi Model Manager launcher
rem 优先启动打包后的 exe（免 Python 环境）；未打包时回退到源码模式。
cd /d "%~dp0"

if exist "%~dp0dist\PiModelManager.exe" (
    start "" "%~dp0dist\PiModelManager.exe"
    exit /b 0
)

where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "%~dp0src\desktop_app.py"
    exit /b 0
)

where python >nul 2>nul
if %errorlevel%==0 (
    start "" python "%~dp0src\desktop_app.py"
    exit /b 0
)

echo [ERROR] Python not found in PATH.
pause
