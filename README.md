# Finance Sorter

A single-user app for sorting and analysing personal bank transactions. Built around Dutch bank statement exports (ING and ASN), stores everything in a SQLite database you own.

The Flask backend runs on a Raspberry Pi under [App Hub](../App%20Hub/APP-GUIDE.md); a thin Tauri window on Windows points at it, and any browser on the tailnet works just as well. Nothing leaves your own hardware unless you turn on the optional Gemini summaries.

## What it does

- **Import** CSV statements from ING and ASN, dedupe automatically.
- **Categorise** transactions manually, via pattern rules ("anything matching `Albert Heijn` → Boodschappen"), or via the built-in suggestion engine that learns from your history.
- **Match** opposite-direction debits and credits (e.g. P2P reimbursements) with drag-to-pair.
- **Diagnose** spending: anomalies, top merchants, fixed vs. variable spend, salary cadence, topic trends.
- **Track savings**: monthly savings rate history, subscription bloat, fixed costs.
- **Summarise** (optional): generates plain-language digests of recent months via Google Generative AI.

## Install

**The backend**, on the Pi: run `_build_zip.py`, upload `finance-sorter.zip` at `http://<pi-host>:8000`, and set one App Hub Variable — `FINANCE_API_KEY` — if you want the read-only API. Everything else has a working default. Your data lives in `data/finance.db` inside the app directory and survives in-place updates.

Bringing an existing database across: on a fresh install, the login page offers **"Bestaande database uploaden"**. That window closes for good the moment an account exists, so it cannot be used to take over a running install.

**The window**, on Windows: download the latest `Finance.Sorter_X.Y.Z_x64-setup.exe` from the [Releases page](https://github.com/meinteholl/FinanceSorter/releases) and run it. It asks once where the Pi is. The installer is signed; future updates are delivered in-app.

A browser works just as well — the app is a normal website on your tailnet.

## Architecture

The backend runs on the Pi; the Windows app is a window pointed at it.

```
Raspberry Pi
  App Hub  ──supervises──>  finance-sorter (Flask + Jinja + SQLite)
                              ├── data/finance.db          persistent, never in the zip
                              ├── /api/v1/*                read-only, key-authenticated
                              └── bound to 127.0.0.1:<PORT>, fronted over HTTPS by
                                  `tailscale serve` at :<PORT + 5000>

  Mei  ──finance_client.py──> http://127.0.0.1:<PORT>/api/v1   (loopback, never the network)

Windows
  Tauri shell (finance-sorter.exe, ~2 MB)
    └── WebView2 pointed at https://<host>:<PORT + 5000>
```

The shell stores the host in `config.json`, probes `/health` before navigating, and shows its own screen when the Pi is unreachable — once the webview is on the remote origin, a dead backend means WebView2's error page instead. `Ctrl+Shift+H` goes back to the setup screen from anywhere.

The backend used to ship *inside* the installer as a PyInstaller sidecar this process spawned and supervised. Dropping that took the installer from 26.7 MB to 2.2 MB.

Tech:
- **Backend**: Flask, Python 3.11+, SQLite in WAL (stdlib only — no pandas, no numpy)
- **Frontend**: Jinja2 templates + vanilla JS, no bundler, no framework
- **Deployment**: a zip uploaded to App Hub, built by `_build_zip.py`
- **Desktop shell**: Tauri 2 (Rust), `reqwest` for the health probe
- **Auto-updates**: `tauri-plugin-updater`, manifest served from GitHub Releases — note this now ships only the window, not the app

Categorisation still uses a local model (Ollama) for merchants it has never seen. That model stays on a desktop machine and the Pi reaches it over the tailnet; `users.llm_url` points at it. Merchant names therefore travel between your own machines, but never to a third party.

## Development

Prerequisites: Python 3, Rust + `cargo`, the [Tauri CLI](https://tauri.app/), and `gh` for releases.

```cmd
setup.bat       :: creates .venv and installs Python deps
run.bat         :: Flask dev mode on http://127.0.0.1:5004, with the reloader on
_build_zip.py   :: builds finance-sorter.zip for App Hub  (.venv\Scripts\python.exe _build_zip.py)
build.bat       :: Tauri build — produces the signed .exe + .msi
DeployUpdate.bat 0.1.2 "Notes here"   :: bump version, build, tag, push, GitHub release
```

The two halves version independently: `DeployUpdate.bat` ships the window and only touches `tauri.conf.json` and `Cargo.toml`, while `manifest.json` carries the backend's own version and goes out through App Hub's in-place update form. Don't try to keep the numbers in sync.

Environment variables the backend reads, all optional except where noted:

| Variable | Default | Notes |
|---|---|---|
| `PORT`, `APPHUB_BIND_HOST` | `5004`, `127.0.0.1` | Set by App Hub |
| `FINANCE_API_KEY` | unset | Required for `/api/v1`; unset means every route 503s |
| `FINANCE_SECRET_KEY` | generated | Cached in `data/.secret_key` on first run |
| `FINANCE_DATA_DIR` | `data/` next to the source | For running a second copy against a copy of the DB |
| `FINANCE_OLLAMA_URL` | `http://127.0.0.1:11434` | Where this install looks for Ollama. The built-in loopback default is always wrong on the Pi |
| `FINANCE_COOKIE_NAME` | `session_<app dir>` | Derived per install, so two copies on one host cannot collide. Override only if you must |
| `FINANCE_COOKIE_INSECURE` | unset | Only when serving the plain-HTTP port instead of the HTTPS endpoint |
| `FINANCE_DEBUG` | unset | Reloader + traceback console. **Never on the Pi** — it is a remote shell |

`SetupGithub.bat` is a one-time bootstrap that installs `gh`, generates the Ed25519 signing key at `%USERPROFILE%\.tauri\finance-sorter.key`, embeds the pubkey in `tauri.conf.json`, and pushes the repo.

## One install per person

App Hub keys apps by `manifest.json`'s `name` and uses it as the directory, so
each person gets a separate app: own venv, own port, own database, nothing
shared. The instances live in `INSTANCES` at the top of `_build_zip.py`:

| App name | Owner |
|---|---|
| `finance-meinte` | Meinte |
| `finance-jordi` | Jordi |

`_build_zip.py` with no arguments builds every bundle; pass a name to build just
one. The `name` in the checked-in `manifest.json` is a placeholder — it is always
rewritten, so the bundles cannot drift apart in anything but the name and the
card label. Adding a third person is one line plus an upload.

A fresh install starts with an empty database and the first-run window open, so
its owner claims it themselves — either by creating an account or by uploading an
existing `finance.db` on the login page. That window closes for good the moment
an account exists.

Two things worth deciding rather than discovering:

- **Leave `FINANCE_API_KEY` unset** on an install whose owner has no assistant of
  their own. Unset means the whole API stays off.
- **Set `FINANCE_OLLAMA_URL` to that person's own machine.** Tier-3
  categorisation sends merchant names to whatever host it names, so leaving it
  pointed at someone else's desktop routes their statements past it. The
  built-in default is loopback, which on the Pi means nothing answers at all.

The Ollama URL stays editable per person on the Profiel page; the Variable only
supplies the starting value, so nobody has to go find the field before
categorisation works. A value someone has actually chosen there always wins.

Session cookies are scoped by hostname, not port, so two installs on one Pi would
otherwise both write the same cookie name under different signing keys and
quietly log each other out. The cookie name is derived from the app directory to
make that impossible; `FINANCE_COOKIE_NAME` overrides it if ever needed.

## Layout

```
app.py                 Flask app: all routes, the categorisation engine, /api/v1
db.py                  SQLite schema + connection
llm_categorize.py      Ollama tier-3 suggestions (stdlib urllib only)
templates/             Jinja2 templates (9 pages)
static/                CSS + vanilla JS
API.md                 The read-only API contract
manifest.json          App Hub app manifest
setup.sh, run.sh       App Hub install + launch hooks (must stay LF)
_build_zip.py          Builds the App Hub bundle from an allow-list
src-tauri/             Rust shell + Tauri config
scripts/               PowerShell helpers for the deploy scripts
data/                  SQLite + session key (gitignored, never bundled)
```

The zip is built from an explicit allow-list rather than an ignore list. An App Hub in-place update overlays the zip onto the live install, so a `data/finance.db` that slipped in would overwrite real financial data with a stale snapshot — the one mistake that cannot be undone by re-uploading. `_build_zip.py` also refuses outright to bundle anything matching a database extension.
