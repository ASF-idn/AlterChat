@echo off
title AlterChat
cd /d "%~dp0"
if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"
python main.py
if errorlevel 1 (
    echo.
    echo AlterChat exited with an error. See the messages above.
    pause
)
