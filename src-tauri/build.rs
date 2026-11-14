fn main() {
    let attributes = tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "install_update",
            "check_desktop_update",
            "restart_app",
            "llm_stream",
            "llm_cancel",
            "check_apk_update",
            "install_apk_update",
        ]),
    );
    tauri_build::try_build(attributes).expect("failed to run tauri-build");
}