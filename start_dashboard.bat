@echo off
cd /d "%~dp0"
start "Delta SuperTrend Dashboard" python -u dashboard.py
timeout /t 3 /nobreak >nul
start "" http://127.0.0.1:8765
