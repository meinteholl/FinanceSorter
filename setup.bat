@echo off
setlocal

cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY where python3 >nul 2>nul && set "PY=python3"

if not defined PY (
    echo [setup] Python is not installed or not on PATH.
    echo         Install Python 3.10+ from https://www.python.org/downloads/
    pause
    exit /b 1
)

echo [setup] Using Python launcher: %PY%

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys" >nul 2>nul || (
        echo [setup] Existing .venv is broken ^(interpreter missing^). Recreating ...
        rmdir /s /q ".venv"
    )
)

if not exist ".venv\Scripts\python.exe" (
    echo [setup] Creating virtual environment in .venv ...
    if exist ".venv" rmdir /s /q ".venv"
    %PY% -m venv .venv
    if errorlevel 1 (
        echo [setup] Failed to create virtual environment.
        pause
        exit /b 1
    )
)

echo [setup] Installing dependencies ...
call ".venv\Scripts\python.exe" -m pip install --upgrade pip
call ".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [setup] Dependency install failed.
    pause
    exit /b 1
)

echo.
echo [setup] Done. Run the app with: run.bat
echo.
pause
endlocal
