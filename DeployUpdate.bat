@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "NEW_VERSION=%~1"
set "NOTES=%~2"

REM Prompt interactively when launched by double-click (no args).
if "%NEW_VERSION%"=="" (
  echo Deploy a new version of Finance Sorter to GitHub.
  echo.
  set /p "NEW_VERSION=Version number (e.g. 0.1.1): "
)
if "%NEW_VERSION%"=="" (
  echo [error] No version provided. Aborting.
  echo.
  pause
  exit /b 1
)
if "%NOTES%"=="" (
  set /p "NOTES=Release notes (optional, press Enter to skip): "
)
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

echo.
echo === Deploying Finance Sorter v%NEW_VERSION% ===
echo.

REM ---- 1. Bump versions in tauri.conf.json + Cargo.toml -----------------------
echo [1/5] Bumping version to %NEW_VERSION%...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\bump-version.ps1" -Version "%NEW_VERSION%"
if errorlevel 1 ( echo [error] version bump failed. & goto :failed )

REM ---- 2. Tauri build with signing -------------------------------------------
REM Route through build-signed.ps1, which sets a genuinely EMPTY password env
REM var via the Win32 API. (Neither cmd nor PowerShell can set an empty env
REM var directly -- both delete it, which Tauri interprets as a prompt
REM request — that's how you'd hit "Decrypting updater signing key, expect a
REM prompt for password" mid-build.)
echo [2/5] Building signed Tauri installer (this takes a few minutes)...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\build-signed.ps1" -KeyPath "%KEY_PATH%"
if errorlevel 1 ( echo [error] cargo tauri build failed. & goto :failed )

REM ---- 3. Build latest.json update manifest ----------------------------------
echo [3/5] Building latest.json...
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

REM ---- 4. Commit, tag, push --------------------------------------------------
echo [4/5] Committing, tagging v%NEW_VERSION%, pushing...
git add ".\src-tauri\tauri.conf.json" ".\src-tauri\Cargo.toml" ".\src-tauri\Cargo.lock" 2>nul
git diff --cached --quiet
if errorlevel 1 (
  git commit -m "Release v%NEW_VERSION%" >nul
  if errorlevel 1 ( echo [error] git commit failed. & goto :failed )
) else (
  echo [4/5]   ^(no version-file changes to commit -- tagging the current HEAD^)
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

REM ---- 5. Create GitHub release ----------------------------------------------
echo [5/5] Creating GitHub release...
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
