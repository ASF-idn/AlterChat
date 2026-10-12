@echo off
setlocal
title AlterChat - Build (PyInstaller --onedir)
:: Always run from the folder containing this script.
cd /d "%~dp0"

set "APP_NAME=AlterChat"
set "DIST_DIR=dist\%APP_NAME%"

echo ========================================================
echo          AlterChat - Build Portable App (--onedir)
echo ========================================================
echo.

:: ------------------------------------------------------------------
:: [1/4] Activate virtual environment & verify PyInstaller
:: ------------------------------------------------------------------
echo [1/4] Preparing build environment...
if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"
python --version >nul 2>&1
if errorlevel 1 goto :no_python
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 python -m pip install pyinstaller
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 goto :no_pyinstaller
echo.

:: ------------------------------------------------------------------
:: [2/4] Clean previous build output
:: ------------------------------------------------------------------
echo [2/4] Cleaning previous build...
if exist "build\%APP_NAME%" rmdir /s /q "build\%APP_NAME%"
if exist "%DIST_DIR%" rmdir /s /q "%DIST_DIR%"
echo.

:: ------------------------------------------------------------------
:: [3/4] Run PyInstaller in --onedir mode (fast startup, no unpacking)
:: ------------------------------------------------------------------
echo [3/4] Running PyInstaller...
python -m PyInstaller main.py ^
    --noconfirm ^
    --clean ^
    --onedir ^
    --windowed ^
    --name "%APP_NAME%" ^
    --collect-all customtkinter ^
    --hidden-import PIL._tkinter_finder
if errorlevel 1 goto :build_failed
echo.

:: ------------------------------------------------------------------
:: [4/4] Place data skeleton and .env.example next to the .exe
:: ------------------------------------------------------------------
echo [4/4] Creating data skeleton next to %APP_NAME%.exe...
mkdir "%DIST_DIR%\data\avatars" 2>nul
mkdir "%DIST_DIR%\data\history" 2>nul

if exist ".env" (
    copy /y ".env" "%DIST_DIR%\" >nul
) else if exist ".env.example" (
    copy /y ".env.example" "%DIST_DIR%\.env" >nul
)

if exist "README.md" copy /y "README.md" "%DIST_DIR%\" >nul
:: ------------------------------------------------------------------
:: [5/5] Clean up temporary build folder & finish
:: ------------------------------------------------------------------
if exist "build" rmdir /s /q "build"

echo.
echo ========================================================
echo   Build complete!
echo ========================================================
echo   Executable location: %CD%\%DIST_DIR%\%APP_NAME%.exe
echo.
echo   [PENTING] Jalankan aplikasi DARI folder 'dist\%APP_NAME%',
echo             BUKAN dari folder 'build' (folder build hanya cache sementara).
echo.
explorer "%DIST_DIR%"
pause
exit /b 0

:: ------------------------------------------------------------------
:: Error handlers
:: ------------------------------------------------------------------
:no_python
echo [ERROR] Python not found. Run setup.bat first.
pause
exit /b 1

:no_pyinstaller
echo [ERROR] PyInstaller could not be installed.
pause
exit /b 1

:build_failed
echo.
echo [ERROR] PyInstaller build failed. See the log above.
pause
exit /b 1
