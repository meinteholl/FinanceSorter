# Finance Sorter

A single-user desktop app for sorting and analysing personal bank transactions. Built around Dutch bank statement exports (ING and ASN), runs entirely offline on Windows, stores everything in a local SQLite database.

## What it does

- **Import** CSV statements from ING and ASN, dedupe automatically.
- **Categorise** transactions manually, via pattern rules ("anything matching `Albert Heijn` → Boodschappen"), or via the built-in suggestion engine that learns from your history.
- **Match** opposite-direction debits and credits (e.g. P2P reimbursements) with drag-to-pair.
- **Diagnose** spending: anomalies, top merchants, fixed vs. variable spend, salary cadence, topic trends.
- **Track savings**: monthly savings rate history, subscription bloat, fixed costs.
- **Summarise** (optional): generates plain-language digests of recent months via Google Generative AI.

## Install

Download the latest `Finance.Sorter_X.Y.Z_x64-setup.exe` from the [Releases page](https://github.com/meinteholl/FinanceSorter/releases) and run it. The installer is signed; future updates are delivered in-app — you'll get a Dutch dialog asking whether to install when a new version is published.

Your data lives at `%APPDATA%\FinanceSorter\finance.db` and survives reinstall.

## Architecture

The app is a Flask server wrapped in a Tauri (Rust + WebView2) window:

```
Tauri shell (finance-sorter.exe)
  └── spawns PyInstaller sidecar (finance-sorter-backend.exe)
        └── Flask + Jinja templates + SQLite at %APPDATA%\FinanceSorter\
```

Tauri picks a free ephemeral port, spawns the sidecar with `--port` and `--parent-pid`, polls for readiness, then loads `http://127.0.0.1:<port>` in WebView2. The sidecar's parent-PID watchdog ensures it self-terminates if the shell dies — no orphan processes.

Tech:
- **Backend**: Flask, Python 3.x, SQLite (stdlib only — no pandas, no numpy)
- **Frontend**: Jinja2 templates + vanilla JS, no bundler, no framework
- **Desktop shell**: Tauri 2 (Rust)
- **Packaging**: PyInstaller (sidecar), `cargo tauri build` (NSIS + MSI installers)
- **Auto-updates**: `tauri-plugin-updater`, manifest served from GitHub Releases

## Development

Prerequisites: Python 3, Rust + `cargo`, the [Tauri CLI](https://tauri.app/), and `gh` for releases.

```cmd
setup.bat       :: creates .venv and installs Python deps
run.bat         :: Flask dev mode — opens http://127.0.0.1:5004 in your browser
build.bat       :: full Tauri build — produces signed .exe + .msi installer
DeployUpdate.bat 0.1.2 "Notes here"   :: bump version, build, tag, push, GitHub release
```

`SetupGithub.bat` is a one-time bootstrap that installs `gh`, generates the Ed25519 signing key at `%USERPROFILE%\.tauri\finance-sorter.key`, embeds the pubkey in `tauri.conf.json`, and pushes the repo.

## Layout

```
app.py                 Flask app, all routes and the categorisation engine
db.py                  SQLite schema + connection
templates/             Jinja2 templates (9 pages)
static/                CSS + vanilla JS (~1.7k lines)
src-tauri/             Rust shell + Tauri config + sidecar binaries
scripts/               PowerShell helpers for the deploy scripts
data/                  Dev-mode SQLite (gitignored)
```
