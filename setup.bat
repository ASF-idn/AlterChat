@echo off
setlocal
title AlterChat - Automated Setup
:: Always run from the folder containing this script (works when double-clicked).
cd /d "%~dp0"

echo ========================================================
echo               AlterChat - Automated Setup
echo ========================================================
echo.

:: ------------------------------------------------------------------
:: [1/4] Check Python
:: ------------------------------------------------------------------
echo [1/4] Checking Python installation...
python --version >nul 2>&1
if errorlevel 1 goto :no_python
python --version
echo.

:: ------------------------------------------------------------------
:: [2/4] Virtual environment
:: ------------------------------------------------------------------
echo [2/4] Preparing virtual environment...
if exist ".venv\Scripts\activate.bat" goto :activate_venv
echo       Creating .venv ...
python -m venv .venv
if errorlevel 1 goto :venv_failed

:activate_venv
call ".venv\Scripts\activate.bat"
echo       Virtual environment activated.
goto :upgrade_pip

:venv_failed
echo       [WARNING] Could not create .venv - installing into the global Python instead.

:upgrade_pip
python -m pip install --upgrade pip --quiet
echo.

:: ------------------------------------------------------------------
:: [3/4] Install dependencies (openai, python-dotenv, customtkinter, etc.)
:: ------------------------------------------------------------------
echo [3/4] Installing dependencies from requirements.txt...
python -m pip install -r requirements.txt
if errorlevel 1 goto :reqs_failed
if not exist "data\avatars" mkdir "data\avatars"
if not exist "data\history" mkdir "data\history"
echo.

:: ------------------------------------------------------------------
:: [4/4] Environment configuration (.env)
:: ------------------------------------------------------------------
echo [4/4] Checking API configuration (.env)...
if not exist ".env" (
    if exist ".env.example" copy /y ".env.example" ".env" >nul
    echo.
    echo       [ACTION REQUIRED] A template '.env' file has been created.
    echo       Open '.env' in Notepad and enter your CUSTOM_API_KEY before running.
) else (
    echo       .env file detected.
)

echo.
echo ========================================================
echo                   Setup Complete!
echo ========================================================
echo.
echo   1. Ensure your CUSTOM_API_KEY is saved in .env
echo   2. Double-click run.bat to start AlterChat.
echo.
pause
exit /b 0

:: ------------------------------------------------------------------
:: Error handlers
:: ------------------------------------------------------------------
:no_python
echo [ERROR] Python was not found in PATH.
echo         Install Python 3.10 - 3.12 from https://www.python.org/downloads/
echo         and tick "Add Python to PATH" during installation.
echo.
pause
exit /b 1

:reqs_failed
echo.
echo [ERROR] Failed to install packages from requirements.txt.
echo.
pause
exit /b 1
