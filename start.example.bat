@echo off
chcp 65001 >nul 2>&1
title Translation Bench

set "APP_PORT=9000"
echo Starting Translation Bench at http://127.0.0.1:%APP_PORT%
start "" "http://127.0.0.1:%APP_PORT%"
cd /d "%~dp0"
where py >nul 2>&1
if not errorlevel 1 (
  py -3 app.py %APP_PORT%
) else (
  python app.py %APP_PORT%
)
