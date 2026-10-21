@echo off
rem Starts KIO1. Double-click this file, or run it from a terminal.
rem Everything it does is in start.py; this only finds a Python to run it with.
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 start.py %*
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo Python 3.10 or newer is needed: https://www.python.org/downloads/
        echo Tick "Add python.exe to PATH" during installation, then run this again.
        pause
        exit /b 1
    )
    python start.py %*
)

if errorlevel 1 pause
