fn main() {
    // Declaring the app manifest generates an `allow-<command>` permission for
    // each command below, which capabilities/ then grants. Without that the ACL
    // rejects the call outright.
    //
    // It matters for two different origins here. The bundled setup page runs on
    // tauri.localhost and uses the host-configuration commands; the app's own
    // pages are served from the Pi, which Tauri treats as a remote origin, and
    // remote origins can never reach custom commands unless a capability
    // explicitly grants them (see capabilities/remote-app.json).
    tauri_build::try_build(
        tauri_build::Attributes::new().app_manifest(
            tauri_build::AppManifest::new().commands(&[
                "check_for_update_now",
                "get_host",
                "resolve_url",
                "probe_host",
                "set_host",
                "open_stored",
                "show_config",
            ]),
        ),
    )
    .expect("failed to run tauri-build");
}
