fn main() {
    // Declaring the app manifest generates an `allow-check-for-update-now`
    // permission for the command below. Without it the ACL rejects the call:
    // the UI is served from the Flask sidecar over http://localhost, which
    // Tauri treats as a *remote* origin, and remote origins can never reach
    // custom commands unless a capability explicitly grants them.
    tauri_build::try_build(
        tauri_build::Attributes::new()
            .app_manifest(tauri_build::AppManifest::new().commands(&["check_for_update_now"])),
    )
    .expect("failed to run tauri-build");
}
