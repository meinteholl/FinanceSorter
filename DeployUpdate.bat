@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

if "%~1"=="" (
  echo Usage: DeployUpdate.bat ^<new-version^> [release-notes]
  echo Example: DeployUpdate.bat 0.1.1 "Fixed drag-drop on transactions page"
  echo.
  pause
  exit /b 1
)
set "NEW_VERSION=%~1"
set "NOTES=%~2"
if "%NOTES%"=="" set "NOTES=Update to v%NEW_VERSION%."

set "KEY_PATH=%USERPROFILE%\.tauri\finance-sorter.key"

REM ---- preflight checks ------------------------------------------------------
if not exist "%KEY_PATH%" (
  echo [error] Signing key not found at %KEY_PATH%.
  echo         Run SetupGithub.bat first to generate it.
  goto :failed
)

where gh >nul 2>&1
if errorlevel 1 ( echo [error] gh not found. Run SetupGithub.bat first. & goto :failed )

gh auth status >nul 2>&1
if errorlevel 1 ( echo [error] gh not authenticated. Run:  gh auth login  & goto :failed )

if not exist ".venv\Scripts\pyinstaller.exe" (
  echo [error] pyinstaller missing in .venv. Run:
  echo     .venv\Scripts\pip.exe install pyinstaller
  goto :failed
)

echo.
echo === Deploying Finance Sorter v%NEW_VERSION% ===
echo.

REM ---- 1. Bump versions in tauri.conf.json + Cargo.toml -----------------------
echo [1/6] Bumping version to %NEW_VERSION%...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\bump-version.ps1" -Version "%NEW_VERSION%"
if errorlevel 1 ( echo [error] version bump failed. & goto :failed )

REM ---- 2. Rebuild Flask sidecar with PyInstaller -----------------------------
echo [2/6] Rebuilding Flask sidecar...
call ".venv\Scripts\pyinstaller.exe" --onefile --name finance-sorter-backend --add-data "templates;templates" --add-data "static;static" --console --noconfirm app.py >nul 2>&1
if errorlevel 1 ( echo [error] PyInstaller failed. Run it manually to see why. & goto :failed )
copy /Y ".\dist\finance-sorter-backend.exe" ".\src-tauri\binaries\finance-sorter-backend-x86_64-pc-windows-msvc.exe" >nul
if errorlevel 1 ( echo [error] could not stage sidecar. & goto :failed )

REM ---- 3. Tauri build with signing -------------------------------------------
echo [3/6] Building signed Tauri installer (this takes a few minutes)...
set "TAURI_SIGNING_PRIVATE_KEY=%KEY_PATH%"
set "TAURI_SIGNING_PRIVATE_KEY_PASSWORD="
cargo tauri build
if errorlevel 1 ( echo [error] cargo tauri build failed. & goto :failed )

REM ---- 4. Build latest.json update manifest ----------------------------------
echo [4/6] Building latest.json...
set "BUNDLE_DIR=.\src-tauri\target\release\bundle\nsis"
set "INSTALLER=%BUNDLE_DIR%\Finance Sorter_%NEW_VERSION%_x64-setup.exe"
set "SIG_FILE=%INSTALLER%.sig"
set "RENAMED=%BUNDLE_DIR%\Finance.Sorter_%NEW_VERSION%_x64-setup.exe"
set "MANIFEST=%BUNDLE_DIR%\latest.json"
set "DOWNLOAD_URL=https://github.com/meinteholl/FinanceSorter/releases/download/v%NEW_VERSION%/Finance.Sorter_%NEW_VERSION%_x64-setup.exe"

if not exist "%INSTALLER%" ( echo [error] Installer not found: "%INSTALLER%" & goto :failed )
if not exist "%SIG_FILE%"  ( echo [error] Signature not found: "%SIG_FILE%" -- is the signing key wired up? & goto :failed )

copy /Y "%INSTALLER%" "%RENAMED%" >nul
if errorlevel 1 ( echo [error] could not rename installer. & goto :failed )

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\build-latest-json.ps1" -Version "%NEW_VERSION%" -Notes "%NOTES%" -SigFile "%SIG_FILE%" -DownloadUrl "%DOWNLOAD_URL%" -OutFile "%MANIFEST%"
if errorlevel 1 ( echo [error] latest.json build failed. & goto :failed )

REM ---- 5. Commit, tag, push --------------------------------------------------
echo [5/6] Committing, tagging v%NEW_VERSION%, pushing...
git add ".\src-tauri\tauri.conf.json" ".\src-tauri\Cargo.toml" ".\src-tauri\Cargo.lock" 2>nul
git diff --cached --quiet
if errorlevel 1 (
  git commit -m "Release v%NEW_VERSION%" >nul
  if errorlevel 1 ( echo [error] git commit failed. & goto :failed )
) else (
  echo [5/6]   ^(no version-file changes to commit -- tagging the current HEAD^)
)

git rev-parse "v%NEW_VERSION%" >nul 2>&1
if not errorlevel 1 (
  echo [error] Tag v%NEW_VERSION% already exists. Pick a higher version or delete the tag.
  goto :failed
)
git tag "v%NEW_VERSION%"
if errorlevel 1 ( echo [error] git tag failed. & goto :failed )

git push origin main
if errorlevel 1 ( echo [error] git push origin main failed. & goto :failed )

git push origin "v%NEW_VERSION%"
if errorlevel 1 ( echo [error] git push tag failed. & goto :failed )

REM ---- 6. Create GitHub release ----------------------------------------------
echo [6/6] Creating GitHub release...
gh release create "v%NEW_VERSION%" "%RENAMED%" "%MANIFEST%" --title "v%NEW_VERSION%" --notes "%NOTES%"
if errorlevel 1 (
  echo [error] gh release create failed. The tag is already pushed -- fix the issue and run:
  echo     gh release create v%NEW_VERSION% "%RENAMED%" "%MANIFEST%" --title v%NEW_VERSION%
  goto :failed
)

echo.
echo === v%NEW_VERSION% released. ===
echo   Installer: %RENAMED%
echo   Manifest:  %MANIFEST%
echo   GitHub:    https://github.com/meinteholl/FinanceSorter/releases/tag/v%NEW_VERSION%
echo.
echo Anyone running an older version will get a "Versie %NEW_VERSION% is beschikbaar"
echo prompt the next time they launch the app.
goto :done

:failed
echo.
echo *** Deploy did not complete. Read the [error] line above. ***
echo.
pause
endlocal
exit /b 1

:done
echo.
pause
endlocal
exit /b 0
