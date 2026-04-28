@echo off
setlocal

cd /d "%~dp0"

where py >nul 2>nul
if not errorlevel 1 (
    py -3.12 -V >nul 2>nul
    if not errorlevel 1 (
        set PYTHON_CMD=py -3.12
    ) else (
        set PYTHON_CMD=python
    )
) else (
    set PYTHON_CMD=python
)

%PYTHON_CMD% -m scopesuite_v3.self_tests
if errorlevel 1 (
    echo.
    echo FlukeScopeSuite self-tests failed.
    exit /b 1
)

%PYTHON_CMD% -m PyInstaller ^
--noconfirm ^
--clean ^
--onefile ^
--windowed ^
--name FlukeScopeSuiteV2AutoTune ^
--hidden-import scopesuite_v3.autotune_engine ^
--hidden-import scopesuite_v3.app ^
--hidden-import serial ^
--hidden-import serial.tools.list_ports ^
--hidden-import numpy ^
--hidden-import matplotlib ^
--hidden-import matplotlib.backends.backend_agg ^
--collect-data matplotlib ^
FlukeScopeSuite_Pro_v3.py

if errorlevel 1 (
    echo.
    echo FlukeScopeSuite V2 AutoTune build failed.
    exit /b 1
)

echo.
echo Build complete: dist\FlukeScopeSuiteV2AutoTune.exe
