// Tauri shell: a window pointed at a Finance Sorter running somewhere else.
//
// The backend used to ship inside this installer as a PyInstaller sidecar that
// this process spawned, health-polled and killed on close. It now runs on the
// user's Raspberry Pi under App Hub, so all of that is gone. What is left is a
// window, a remembered host, and the updater.
//
// Startup:
//   1. Show the bundled page immediately — it starts on its "connecting" state,
//      which is the same spinner the old build showed while the sidecar booted.
//   2. That page asks for the stored host. None yet: it shows the setup form.
//   3. Otherwise it probes /health through us and, on success, has us navigate
//      to the real app.
//
// The probe happens before we navigate on purpose. Once the webview is on the
// remote origin a dead Pi means WebView2's own error page, which we cannot
// style, cannot translate, and cannot offer a way out of.

use std::fs;
use std::path::PathBuf;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager, Url, WebviewWindow};

// ---------------------------------------------------------------------------
// Stored host
// ---------------------------------------------------------------------------

#[derive(Serialize, Deserialize, Default, Clone)]
struct AppConfig {
    host: Option<String>,
}

fn config_path(app: &AppHandle) -> PathBuf {
    let dir = app.path().app_config_dir().expect("no config dir");
    fs::create_dir_all(&dir).ok();
    dir.join("config.json")
}

fn load_config(app: &AppHandle) -> AppConfig {
    fs::read_to_string(config_path(app))
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default()
}

fn save_config(app: &AppHandle, config: &AppConfig) {
    if let Ok(data) = serde_json::to_string_pretty(config) {
        fs::write(config_path(app), data).ok();
    }
}

/// Turn what the user typed into a base URL.
///
/// An explicit scheme always wins. Otherwise: a bare IPv4 literal, `localhost`
/// or a `.local` name is plain HTTP; any other dotted DNS name is HTTPS.
///
/// A port is deliberately *not* treated as evidence of scheme. App Hub exposes
/// each app over TLS at `https://<host>:<local port + 5000>`, so the tailnet
/// address that matters here — `pi.tail1a2b3c.ts.net:10001` — is HTTPS despite
/// carrying a port. Keying off the colon (as the sibling Mei desktop wrapper
/// does) sends exactly that address to http, where nothing answers.
///
/// No heuristic gets every address right, which is why the setup screen renders
/// the resolved URL live: a wrong guess is visible before it is saved, and
/// pasting a full https:// URL always wins outright.
fn base_url(host: &str) -> String {
    let host = host.trim().trim_end_matches('/');
    if host.starts_with("http://") || host.starts_with("https://") {
        return host.to_string();
    }

    let name = host.split(':').next().unwrap_or(host).to_ascii_lowercase();
    let is_ipv4 = name.split('.').count() == 4
        && name
            .split('.')
            .all(|part| !part.is_empty() && part.chars().all(|c| c.is_ascii_digit()));
    let is_local = name == "localhost" || name.ends_with(".local");

    if is_ipv4 || is_local {
        format!("http://{host}")
    } else {
        format!("https://{host}")
    }
}

// ---------------------------------------------------------------------------
// Title bar colour (Windows)
// ---------------------------------------------------------------------------

// Paint the Win11 caption bar to match the Flask app's palette so the native
// title bar blends into the page. Attributes are no-ops on Win10 / older — DWM
// just returns S_FALSE and the system theme keeps applying.
//
// COLORREF is little-endian 0x00BBGGRR; the values here are the byte-reversed
// form of the CSS hex codes in static/style.css.
#[cfg(target_os = "windows")]
fn apply_titlebar_colors(window: &WebviewWindow) {
    use std::ffi::c_void;
    type HWND = *mut c_void;
    type HRESULT = i32;

    const DWMWA_BORDER_COLOR: u32 = 34;
    const DWMWA_CAPTION_COLOR: u32 = 35;
    const DWMWA_TEXT_COLOR: u32 = 36;

    #[link(name = "dwmapi")]
    extern "system" {
        fn DwmSetWindowAttribute(
            hwnd: HWND,
            attr: u32,
            value: *const c_void,
            size: u32,
        ) -> HRESULT;
    }

    let Ok(hwnd) = window.hwnd() else { return };
    let hwnd = hwnd.0 as HWND;

    // #fffbf3 → 0x00F3FBFF (the --bg cream)
    let caption: u32 = 0x00F3FBFF;
    // #564d3e → 0x003E4D56 (the --text warm brown)
    let text: u32 = 0x003E4D56;
    // #BFB18E → 0x008EB1BF (the --rule border)
    let border: u32 = 0x008EB1BF;

    unsafe {
        let p = |v: &u32| v as *const u32 as *const c_void;
        DwmSetWindowAttribute(hwnd, DWMWA_CAPTION_COLOR, p(&caption), 4);
        DwmSetWindowAttribute(hwnd, DWMWA_TEXT_COLOR, p(&text), 4);
        DwmSetWindowAttribute(hwnd, DWMWA_BORDER_COLOR, p(&border), 4);
    }
}

#[cfg(not(target_os = "windows"))]
fn apply_titlebar_colors(_: &WebviewWindow) {}

// ---------------------------------------------------------------------------
// Updater
// ---------------------------------------------------------------------------

// Shared by the startup check and the Profiel page's button. Returns a short
// status the caller can report: whether an update was offered, whether we are
// already current, or why the check could not run.
//
// Only compiled into release builds. `cargo tauri dev` skips it so we don't
// spam GitHub on every reload.
#[cfg(not(debug_assertions))]
async fn run_update_check(app: tauri::AppHandle) -> Result<&'static str, String> {
    use tauri_plugin_dialog::{DialogExt, MessageDialogButtons};
    use tauri_plugin_updater::UpdaterExt;

    let updater = app
        .updater()
        .map_err(|e| format!("Updater niet beschikbaar: {e}"))?;

    let update = match updater.check().await {
        Ok(Some(u)) => u,
        Ok(None) => return Ok("up-to-date"),
        Err(e) => return Err(format!("Controleren mislukt: {e}")),
    };

    let version = update.version.clone();
    let install = app
        .dialog()
        .message(format!(
            "Versie {version} is beschikbaar. Nu installeren? De app start daarna opnieuw op."
        ))
        .title("Update beschikbaar")
        .buttons(MessageDialogButtons::OkCancelCustom(
            "Installeren".into(),
            "Later".into(),
        ))
        .blocking_show();

    if !install {
        return Ok("declined");
    }

    match update.download_and_install(|_chunk, _total| {}, || {}).await {
        // Never actually returns — the app restarts into the new version.
        Ok(_) => {
            app.restart();
        }
        Err(e) => Err(format!("Installeren mislukt: {e}")),
    }
}

// Background update check on launch. Failures are silent — offline / endpoint
// down shouldn't disrupt the user's session.
#[cfg(not(debug_assertions))]
fn check_for_update(app: tauri::AppHandle) {
    tauri::async_runtime::spawn(async move {
        let _ = run_update_check(app).await;
    });
}

#[cfg(debug_assertions)]
fn check_for_update(_: tauri::AppHandle) {}

// On-demand check, invoked from the Profiel page. Unlike the startup check
// this reports back, so the button can say "je hebt al de nieuwste versie"
// instead of appearing to do nothing.
#[tauri::command]
async fn check_for_update_now(app: tauri::AppHandle) -> Result<String, String> {
    #[cfg(not(debug_assertions))]
    {
        run_update_check(app).await.map(|s| s.to_string())
    }
    #[cfg(debug_assertions)]
    {
        let _ = app;
        Ok("dev".to_string())
    }
}

// ---------------------------------------------------------------------------
// Commands used by the bundled setup page
// ---------------------------------------------------------------------------

#[tauri::command]
fn get_host(app: AppHandle) -> Option<String> {
    load_config(&app).host
}

/// What `host` will actually be opened as. Drives the live preview under the
/// input, so the scheme heuristic never surprises anyone.
#[tauri::command]
fn resolve_url(host: String) -> String {
    base_url(&host)
}

/// Ask the backend whether it is there, and say something useful when it isn't.
///
/// Done here rather than with `fetch()` from the page because the bundled page
/// is served from `tauri.localhost`: a request to the tailnet is cross-origin
/// and opaque, so a refused connection and an expired certificate look
/// identical to JavaScript. reqwest gives us the real reason to show.
#[tauri::command]
async fn probe_host(host: String) -> Result<u16, String> {
    let url = format!("{}/health", base_url(&host));

    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(5))
        .build()
        .map_err(|e| format!("Kon geen verbinding opzetten: {e}"))?;

    match client.get(&url).send().await {
        Ok(resp) => Ok(resp.status().as_u16()),
        Err(e) => Err(if e.is_timeout() {
            "Geen antwoord binnen 5 seconden. Staat de Pi aan?".to_string()
        } else if e.is_connect() {
            format!("Geen verbinding met {url}. Ben je met Tailscale verbonden?")
        } else {
            format!("Verbinding mislukt: {e}")
        }),
    }
}

fn navigate_to(app: &AppHandle, host: &str) -> Result<(), String> {
    let url = Url::parse(&base_url(host)).map_err(|e| e.to_string())?;
    let window = app
        .get_webview_window("main")
        .ok_or_else(|| "Geen venster gevonden.".to_string())?;
    window.navigate(url).map_err(|e| e.to_string())
}

/// Persist the host and open it. Validated before saving — a bad host written
/// to config would leave the app stuck on every subsequent launch.
#[tauri::command]
fn set_host(app: AppHandle, host: String) -> Result<(), String> {
    let host = host.trim().trim_end_matches('/').to_string();
    if host.is_empty() {
        return Err("Vul een adres in.".to_string());
    }
    Url::parse(&base_url(&host)).map_err(|e| format!("Ongeldig adres: {e}"))?;

    save_config(&app, &AppConfig { host: Some(host.clone()) });
    navigate_to(&app, &host)
}

/// Open the stored host without re-saving it.
#[tauri::command]
fn open_stored(app: AppHandle) -> Result<(), String> {
    match load_config(&app).host {
        Some(host) => navigate_to(&app, &host),
        None => Err("Er is nog geen adres ingesteld.".to_string()),
    }
}

/// Escape hatch back to the bundled setup page.
///
/// Once the webview is on the remote origin and the Pi disappears mid-session,
/// WebView2 shows its own error page and no Tauri event fires — so there is no
/// way back other than one the user can trigger. Bound to Ctrl+Shift+H by the
/// page itself, which works from the remote origin too.
#[tauri::command]
fn show_config(app: AppHandle) -> Result<(), String> {
    // Windows serves bundled assets over http://tauri.localhost; the other
    // platforms use the tauri:// custom scheme.
    #[cfg(target_os = "windows")]
    let builtin = "http://tauri.localhost/index.html";
    #[cfg(not(target_os = "windows"))]
    let builtin = "tauri://localhost/index.html";

    let url = Url::parse(builtin).map_err(|e| e.to_string())?;
    let window = app
        .get_webview_window("main")
        .ok_or_else(|| "Geen venster gevonden.".to_string())?;
    window.navigate(url).map_err(|e| e.to_string())
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .invoke_handler(tauri::generate_handler![
            check_for_update_now,
            get_host,
            resolve_url,
            probe_host,
            set_host,
            open_stored,
            show_config,
        ])
        .setup(|app| {
            if let Some(window) = app.get_webview_window("main") {
                // Paint the caption before the window is shown, so the user
                // never sees a flash of the default system title bar.
                apply_titlebar_colors(&window);
                let _ = window.show();
                let _ = window.set_focus();
            }

            // No-op in dev builds.
            check_for_update(app.handle().clone());

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

#[cfg(test)]
mod tests {
    use super::base_url;

    #[test]
    fn explicit_scheme_always_wins() {
        assert_eq!(base_url("https://example.test:1234"), "https://example.test:1234");
        assert_eq!(base_url("http://example.test"), "http://example.test");
        // Trailing slash is trimmed so paths concatenate cleanly.
        assert_eq!(base_url("https://example.test/"), "https://example.test");
    }

    #[test]
    fn a_port_is_not_evidence_of_scheme() {
        // The regression this function exists for: App Hub's TLS endpoint is
        // https://<host>:<local port + 5000>, so treating the colon as a signal
        // for http (as the sibling Mei wrapper does) sends the real address to
        // a port where nothing is listening.
        assert_eq!(
            base_url("pi.tail1a2b3c.ts.net:10001"),
            "https://pi.tail1a2b3c.ts.net:10001"
        );
    }

    #[test]
    fn bare_dns_names_are_https() {
        assert_eq!(base_url("pi.tail1a2b3c.ts.net"), "https://pi.tail1a2b3c.ts.net");
    }

    #[test]
    fn ip_literals_and_local_names_are_http() {
        assert_eq!(base_url("192.168.1.42:5001"), "http://192.168.1.42:5001");
        assert_eq!(base_url("192.168.1.42"), "http://192.168.1.42");
        assert_eq!(base_url("localhost:5004"), "http://localhost:5004");
        assert_eq!(base_url("raspberrypi.local"), "http://raspberrypi.local");
        assert_eq!(base_url("RaspberryPi.Local"), "http://RaspberryPi.Local");
    }

    #[test]
    fn whitespace_is_tolerated() {
        assert_eq!(base_url("  pi.tail1a2b3c.ts.net:10001  "),
                   "https://pi.tail1a2b3c.ts.net:10001");
    }

    #[test]
    fn not_four_numeric_octets_is_a_dns_name() {
        // "1.2.3" and "10.0.0.1.example.com" must not be mistaken for IPs.
        assert_eq!(base_url("1.2.3"), "https://1.2.3");
        assert_eq!(base_url("10.0.0.1.example.com"), "https://10.0.0.1.example.com");
    }
}
