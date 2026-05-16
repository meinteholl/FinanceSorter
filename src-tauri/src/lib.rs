// Tauri shell that owns the window and supervises the bundled Flask sidecar.
//
// Lifecycle:
//   1. Pick a free localhost port (avoid 5004 collisions with anything the
//      user might already have on that port).
//   2. Spawn `finance-sorter-backend --port <port>` as a Tauri sidecar.
//   3. TCP-poll 127.0.0.1:<port> until it accepts a connection (Werkzeug only
//      starts accept() once the WSGI app is wired up, so this is a sufficient
//      readiness check — no HTTP client dependency needed).
//   4. Navigate the hidden main window to http://127.0.0.1:<port> and show it.
//   5. On window close, kill the child process so it doesn't outlive the UI.

use std::net::{SocketAddr, TcpListener, TcpStream};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{Emitter, Manager, RunEvent, WebviewWindow, WindowEvent};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

struct SidecarChild(Mutex<Option<CommandChild>>);

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

// Background update check. Hits the configured endpoint, prompts the user via
// a native dialog if a newer version exists, and restarts the app after a
// successful download+install. Failures are silent — offline / endpoint down
// shouldn't disrupt the user's session.
//
// Only compiled into release builds. `cargo tauri dev` skips it so we don't
// spam GitHub on every reload.
#[cfg(not(debug_assertions))]
fn check_for_update(app: tauri::AppHandle) {
    use tauri_plugin_dialog::{DialogExt, MessageDialogButtons};
    use tauri_plugin_updater::UpdaterExt;

    tauri::async_runtime::spawn(async move {
        let Ok(updater) = app.updater() else { return };
        let update = match updater.check().await {
            Ok(Some(u)) => u,
            _ => return,
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
            return;
        }

        if update
            .download_and_install(|_chunk, _total| {}, || {})
            .await
            .is_ok()
        {
            app.restart();
        }
    });
}

#[cfg(debug_assertions)]
fn check_for_update(_: tauri::AppHandle) {}

fn pick_free_port() -> u16 {
    let listener = TcpListener::bind("127.0.0.1:0").expect("could not bind ephemeral port");
    let port = listener.local_addr().unwrap().port();
    drop(listener);
    port
}

fn wait_for_port(port: u16, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    let addr: SocketAddr = format!("127.0.0.1:{port}").parse().unwrap();
    while Instant::now() < deadline {
        if TcpStream::connect_timeout(&addr, Duration::from_millis(250)).is_ok() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    false
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(SidecarChild(Mutex::new(None)))
        .setup(|app| {
            // Paint the caption to match #fffbf3 before anything else happens —
            // the window is still hidden, so the user never sees a flash of the
            // default system title bar.
            if let Some(window) = app.get_webview_window("main") {
                apply_titlebar_colors(&window);
            }

            // Kick off update check in the background; result lands as a native
            // dialog if there's something to install. No-op in dev builds.
            check_for_update(app.handle().clone());

            let port = pick_free_port();

            // Spawn the bundled PyInstaller binary as a sidecar.
            // --parent-pid: PyInstaller --onefile bootloader spawns a child Python
            // process. Killing the bootloader (what Tauri's child.kill() does) does
            // not propagate to the Python child. The child watches this PID and
            // self-terminates when we go away — covers both window close and force-quit.
            let parent_pid = std::process::id().to_string();
            let sidecar = app
                .shell()
                .sidecar("finance-sorter-backend")
                .expect("sidecar binary 'finance-sorter-backend' missing — did `cargo tauri build` skip externalBin?")
                .args(["--port", &port.to_string(), "--parent-pid", &parent_pid]);

            let (mut rx, child) = sidecar.spawn().expect("failed to spawn sidecar");
            app.state::<SidecarChild>().0.lock().unwrap().replace(child);

            // Surface sidecar stdout/stderr in dev to make Flask tracebacks visible.
            tauri::async_runtime::spawn(async move {
                while let Some(event) = rx.recv().await {
                    match event {
                        CommandEvent::Stdout(line) | CommandEvent::Stderr(line) => {
                            eprintln!("[sidecar] {}", String::from_utf8_lossy(&line));
                        }
                        CommandEvent::Terminated(payload) => {
                            eprintln!("[sidecar] terminated: code={:?}", payload.code);
                        }
                        _ => {}
                    }
                }
            });

            // Wait for readiness off the main thread, then navigate + show window.
            let app_handle = app.handle().clone();
            std::thread::spawn(move || {
                if !wait_for_port(port, Duration::from_secs(20)) {
                    eprintln!("sidecar did not become ready within 20s");
                    let _ = app_handle.emit("sidecar-failed", ());
                    return;
                }
                if let Some(window) = app_handle.get_webview_window("main") {
                    let url = format!("http://127.0.0.1:{port}");
                    // Use replace() so the stub doesn't end up in WebView history.
                    let _ = window.eval(&format!(
                        "window.location.replace('{}')",
                        url.replace('\'', "\\'")
                    ));
                    let _ = window.show();
                    let _ = window.set_focus();
                }
            });

            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { .. } = event {
                if let Some(state) = window.app_handle().try_state::<SidecarChild>() {
                    if let Some(child) = state.0.lock().unwrap().take() {
                        let _ = child.kill();
                    }
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app_handle, event| {
            // Belt-and-braces: also kill the child on ExitRequested in case the
            // window-close handler didn't fire (e.g. force-quit from the dock).
            if let RunEvent::ExitRequested { .. } = event {
                if let Some(state) = app_handle.try_state::<SidecarChild>() {
                    if let Some(child) = state.0.lock().unwrap().take() {
                        let _ = child.kill();
                    }
                }
            }
        });
}
