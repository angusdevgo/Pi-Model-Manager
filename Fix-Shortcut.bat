@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0fix_shortcut.ps1"
echo.
pause
