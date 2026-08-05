@echo off
REM Starts now_playing.py and nowplaying_poller.py, each in its own window.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python was not found on PATH. Install Python and try again.
    pause
    exit /b 1
)

if not exist "%~dp0.env" (
    echo [ERROR] .env not found next to this script.
    echo         Copy .env.example to .env and fill in your Icecast details first.
    pause
    exit /b 1
)

cd /d "%~dp0"

start "now_playing" cmd /k python now_playing.py
start "nowplaying_poller" cmd /k python nowplaying_poller.py

echo Started now_playing.py and nowplaying_poller.py in separate windows.
