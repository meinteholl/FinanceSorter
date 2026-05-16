@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo === Finance Sorter: one-time GitHub + auto-updater setup ===
echo.

REM ---- 1. Verify required tools ----------------------------------------------
where cargo >nul 2>&1
if errorlevel 1 ( echo [error] cargo not found. Install Rust from https://rustup.rs and re-run. & goto :failed )

where git >nul 2>&1
if errorlevel 1 ( echo [error] git not found. Install from https://git-scm.com and re-run. & goto :failed )

cargo tauri --version >nul 2>&1
if errorlevel 1 ( echo [error] tauri-cli not found. Run:  cargo install tauri-cli --version "^^2" --locked  & goto :failed )

if not exist ".venv\Scripts\python.exe" ( echo [error] .venv not found. Run setup.bat first. & goto :failed )

REM ---- 2. Install GitHub CLI if missing ---------------------------------------
where gh >nul 2>&1
if errorlevel 1 (
  echo [setup] Installing GitHub CLI via winget...
  winget install --id GitHub.cli --silent --accept-package-agreements --accept-source-agreements
  if errorlevel 1 (
    echo [error] winget install failed. Install gh manually from https://cli.github.com and re-run.
    goto :failed
  )
  echo [setup] gh installed. If gh isn't found below, close this window and open a new one ^(PATH refresh^).
)

REM ---- 3. Log into GitHub if needed -------------------------------------------
gh auth status >nul 2>&1
if errorlevel 1 (
  echo [setup] Logging into GitHub. A browser window will open...
  gh auth login
  if errorlevel 1 ( echo [error] gh auth login failed or was cancelled. & goto :failed )
) else (
  echo [setup] gh already authenticated.
)

REM ---- 4. Generate Ed25519 signing keypair ------------------------------------
set "KEY_DIR=%USERPROFILE%\.tauri"
set "KEY_PATH=%KEY_DIR%\finance-sorter.key"
if not exist "%KEY_DIR%" mkdir "%KEY_DIR%"

if exist "%KEY_PATH%" (
  echo [setup] Signing key already exists at %KEY_PATH% -- keeping it.
) else (
  echo [setup] Generating Ed25519 signing key ^(no password, family-only distribution^)...
  cargo tauri signer generate --ci --write-keys "%KEY_PATH%" >nul
  if errorlevel 1 ( echo [error] cargo tauri signer generate failed. & goto :failed )
)
if not exist "%KEY_PATH%.pub" ( echo [error] Public key not produced at %KEY_PATH%.pub & goto :failed )

REM ---- 5. Inject pubkey into tauri.conf.json ----------------------------------
echo [setup] Injecting public key into src-tauri\tauri.conf.json...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\inject-pubkey.ps1" -PubFile "%KEY_PATH%.pub" -ConfFile ".\src-tauri\tauri.conf.json"
if errorlevel 1 ( echo [error] pubkey injection failed. & goto :failed )

REM ---- 6. Initialize git and push to GitHub -----------------------------------
if not exist ".git" (
  echo [setup] Initializing git repo...
  git init >nul
  if errorlevel 1 ( echo [error] git init failed. & goto :failed )
)

git config user.email >nul 2>&1 || git config user.email "meinteholl@gmail.com"
git config user.name  >nul 2>&1 || git config user.name  "meinteholl"

echo [setup] Staging files...
git add -A
git diff --cached --quiet
if errorlevel 1 (
  git commit -m "Initial commit: Finance Sorter desktop app with auto-updater"
  if errorlevel 1 ( echo [error] git commit failed. & goto :failed )
) else (
  echo [setup] Nothing new to commit.
)

git branch -M main >nul 2>&1
git remote get-url origin >nul 2>&1
if errorlevel 1 git remote add origin https://github.com/meinteholl/FinanceSorter.git

echo [setup] Pushing to GitHub...
git push -u origin main 2>nul
if errorlevel 1 (
  echo.
  echo [setup] Push failed -- the GitHub repo probably doesn't exist yet. Creating it via gh...
  gh repo create meinteholl/FinanceSorter --private --source=. --remote=origin --push
  if errorlevel 1 (
    echo [error] gh repo create failed. Create the repo manually at  https://github.com/new
    echo         ^(name it FinanceSorter, do NOT add README, then re-run this script^).
    goto :failed
  )
)

echo.
echo === Setup complete. ===
echo   Signing key:    %KEY_PATH%
echo   Public key:     embedded in src-tauri\tauri.conf.json
echo   Git remote:     https://github.com/meinteholl/FinanceSorter
echo.
echo Next: ship a release with
echo     DeployUpdate.bat 0.1.1
goto :done

:failed
echo.
echo *** Setup did not complete. Read the [error] line above. ***
echo.
pause
endlocal
exit /b 1

:done
echo.
pause
endlocal
exit /b 0
