@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [run] Virtual environment not found. Running setup.bat first ...
    call setup.bat
    if errorlevel 1 exit /b 1
)

echo [run] Starting Finance Sorter on http://127.0.0.1:5004
start "" http://127.0.0.1:5004
".venv\Scripts\python.exe" app.py

endlocal
