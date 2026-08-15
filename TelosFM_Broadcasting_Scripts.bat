@echo off
REM TelosFM broadcasting support launcher.
REM Starts each long-running radio support script in its own console window.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python was not found on PATH. Install Python and try again.
    pause
    exit /b 1
)

where ffmpeg >nul 2>nul
if errorlevel 1 (
    echo [ERROR] FFmpeg was not found on PATH.
    echo         dead_air_monitor.py requires FFmpeg to analyze the live stream.
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

start "TelosFM - Now Playing" cmd /k python now_playing.py
start "TelosFM - Metadata Poller" cmd /k python nowplaying_poller.py
start "TelosFM - Dead Air Monitor" cmd /k python dead_air_monitor.py

echo Started TelosFM broadcasting support scripts:
echo   now_playing.py
echo   nowplaying_poller.py
echo   dead_air_monitor.py
