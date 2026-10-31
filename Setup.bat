@echo off
title Pop Locker - setup
cd /d "%~dp0"
rem setup registers the chat database as a Windows service, which needs administrator rights - ask once
net session >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_claude_voice.ps1"
echo.
pause
