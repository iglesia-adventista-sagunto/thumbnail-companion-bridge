@echo off
cd /d "%~dp0"

if not exist "venv" (
    echo Creating virtual environment...
    python -m venv venv
    call venv\Scripts\python.exe -m pip install --upgrade pip
    call venv\Scripts\pip.exe install -r requirements.txt
)

echo Starting Thumbnail Companion Bridge (port from config.json / PORT env, default 5001)...
call venv\Scripts\python.exe app.py
