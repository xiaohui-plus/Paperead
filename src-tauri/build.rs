use std::env;
use std::fs;
use std::path::{Path, PathBuf};

/// 反馈令牌资源在打包目录里的文件名；复制到 OUT_DIR 时换成下划线，避免
/// `include_bytes!` 路径里出现连字符。
const FEEDBACK_KEY: &str = "feedback-token.key";
const FEEDBACK_BLOB: &str = "feedback-token.bin";

fn stage_feedback_resource(manifest: &Path, out: &Path, name: &str, placeholder: &[u8]) {
    let source = manifest.join("resources").join(name);
    let target = out.join(name.replace('-', "_"));
    if source.is_file() {
        fs::copy(&source, &target).expect("复制反馈凭据失败");
    } else {
        fs::write(&target, placeholder).expect("写入反馈凭据占位失败");
    }
    println!("cargo:rerun-if-changed={}", source.display());
}

fn main() {
    let manifest = PathBuf::from(env::var("CARGO_MANIFEST_DIR").expect("缺少 CARGO_MANIFEST_DIR"));
    let out = PathBuf::from(env::var("OUT_DIR").expect("缺少 OUT_DIR"));

    // 开源仓库里没有这两份文件时写入占位（全 0 密钥 + 空密文），
    // 编译出的壳不带内置通道，用户仍可在设置里填自己的令牌。
    stage_feedback_resource(&manifest, &out, FEEDBACK_KEY, &[0u8; 32]);
    stage_feedback_resource(&manifest, &out, FEEDBACK_BLOB, &[]);

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