@echo off
setlocal
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
    echo Python not found. Install Python 3.10 or newer and enable PATH.
    pause
    exit /b 1
)
echo Creator Workbench: http://127.0.0.1:8754/
start "" "http://127.0.0.1:8754/"
python app.py
if errorlevel 1 pause
