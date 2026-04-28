@echo off
setlocal

cd /d "%~dp0"

python -m PyInstaller ^
--noconfirm ^
--clean ^
--onefile ^
--console ^
--name FlukeScopeMeterAnalyzer ^
--hidden-import serial ^
--hidden-import serial.tools.list_ports ^
--hidden-import numpy ^
--hidden-import pandas ^
--hidden-import openpyxl ^
--hidden-import matplotlib ^
--hidden-import matplotlib.backends.backend_agg ^
--collect-data matplotlib ^
Fluke_Replay_Final_Tool_A_V_B_I.py

if errorlevel 1 (
    echo.
    echo Build failed.
    exit /b 1
)

echo.
echo Build complete: dist\FlukeScopeMeterAnalyzer.exe
