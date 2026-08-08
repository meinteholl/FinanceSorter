@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo === Finance Sorter: local build ===
echo.

REM ---- preflight -------------------------------------------------------------
where cargo >nul 2>&1
if errorlevel 1 ( echo [error] cargo not found. Install Rust from https://rustup.rs and re-run. & goto :failed )

cargo tauri --version >nul 2>&1
if errorlevel 1 ( echo [error] tauri-cli not found. Run:  cargo install tauri-cli --version "^^2" --locked  & goto :failed )

if not exist ".venv\Scripts\pyinstaller.exe" (
  echo [error] pyinstaller missing in .venv. Run:
  echo     .venv\Scripts\pip.exe install pyinstaller
  goto :failed
)

REM ---- 1. Rebuild Flask sidecar ----------------------------------------------
echo [1/2] Rebuilding Flask sidecar with PyInstaller...
call ".venv\Scripts\pyinstaller.exe" --onefile --name finance-sorter-backend --add-data "templates;templates" --add-data "static;static" --console --noconfirm app.py >nul 2>&1
if errorlevel 1 ( echo [error] PyInstaller failed. Run it manually to see why. & goto :failed )

copy /Y ".\dist\finance-sorter-backend.exe" ".\src-tauri\binaries\finance-sorter-backend-x86_64-pc-windows-msvc.exe" >nul
if errorlevel 1 ( echo [error] could not stage sidecar. & goto :failed )

REM ---- 2. Tauri build --------------------------------------------------------
echo [2/2] Building Tauri shell and installer (this takes a few minutes)...

REM If a signing key is present, route the build through build-signed.ps1,
REM which sets a genuinely empty password env var via the Win32 API. (Neither
REM cmd nor PowerShell can set an empty env var directly -- both delete it,
REM which Tauri reads as "prompt me".)
set "KEY_PATH=%USERPROFILE%\.tauri\finance-sorter.key"
if exist "%KEY_PATH%" (
  echo       ^(signing with %KEY_PATH%^)
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\build-signed.ps1" -KeyPath "%KEY_PATH%"
) else (
  echo       ^(no signing key found at %KEY_PATH% -- build will be unsigned^)
  cargo tauri build
)
if errorlevel 1 ( echo [error] cargo tauri build failed. & goto :failed )

echo.
echo === Build complete. ===
echo   Standalone exe:  .\src-tauri\target\release\finance-sorter.exe
echo   MSI installer:   .\src-tauri\target\release\bundle\msi\
echo   NSIS installer:  .\src-tauri\target\release\bundle\nsis\
echo.
echo Double-click the standalone exe to launch without installing.
goto :done

:failed
echo.
echo *** Build did not complete. Read the [error] line above. ***
echo.
pause
endlocal
exit /b 1

:done
echo.
pause
endlocal
exit /b 0
