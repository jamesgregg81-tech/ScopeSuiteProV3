@echo off
setlocal

cd /d "%~dp0"

python -m PyInstaller ^
--noconfirm ^
--clean ^
FlukeScopeMeterAnalyzerGUI.spec

if errorlevel 1 (
    echo.
    echo GUI build failed.
    exit /b 1
)

echo.
echo GUI build complete: dist\FlukeScopeMeterAnalyzerGUI.exe
