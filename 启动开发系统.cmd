@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-dev.ps1"
echo.
echo Server stopped. If there are errors, send a screenshot.
pause
