//! 反馈提交通道的内置凭据。
//!
//! 令牌不在 Python 侧落明文：构建时由 `scripts/pack-feedback-token.py` 用
//! AES-256-GCM 加密成 `resources/feedback-token.bin`，主密钥放在
//! `resources/feedback-token.key`（两者都在已忽略的 resources 目录里）。
//! `build.rs` 会把它们复制进 OUT_DIR 供 `include_bytes!` 编译进壳二进制，
//! 运行时只在内存里解密，再通过 `PAPEREAD_FEEDBACK_TOKEN` 交给 sidecar。
//!
//! 注意：这是客户端内嵌凭据，只能提高提取门槛，无法做到对逆向者绝对保密。

use aes_gcm::aead::{Aead, KeyInit};
use aes_gcm::{Aes256Gcm, Nonce};

const KEY: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/feedback_token.key"));
const BLOB: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/feedback_token.bin"));

const KEY_LEN: usize = 32;
const NONCE_LEN: usize = 12;
const TAG_LEN: usize = 16;

/// 解密内置反馈令牌；没有配置（占位密钥/空密文）或解密失败时返回 None。
pub fn builtin_token() -> Option<String> {
    decrypt(KEY, BLOB)
}

fn decrypt(key: &[u8], blob: &[u8]) -> Option<String> {
    if key.len() != KEY_LEN || blob.len() <= NONCE_LEN + TAG_LEN {
        return None;
    }
    // build.rs 在缺少密钥文件时会写全 0 占位，此时视为「未配置」。
    if key.iter().all(|byte| *byte == 0) {
        return None;
    }
    let cipher = Aes256Gcm::new_from_slice(key).ok()?;
    let (nonce, ciphertext) = blob.split_at(NONCE_LEN);
    let plain = cipher.decrypt(Nonce::from_slice(nonce), ciphertext).ok()?;
    let token = String::from_utf8(plain).ok()?;
    let token = token.trim();
    if token.is_empty() {
        return None;
    }
    Some(token.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn seal(key: &[u8], nonce: &[u8], token: &str) -> Vec<u8> {
        let cipher = Aes256Gcm::new_from_slice(key).unwrap();
        let mut blob = nonce.to_vec();
        blob.extend_from_slice(&cipher.encrypt(Nonce::from_slice(nonce), token.as_bytes()).unwrap());
        blob
    }

    #[test]
    fn decrypts_round_trip() {
        let key = [9u8; KEY_LEN];
        let blob = seal(&key, &[5u8; NONCE_LEN], "  abc123token  ");
        assert_eq!(decrypt(&key, &blob).as_deref(), Some("abc123token"));
    }

    #[test]
    fn rejects_placeholder_key() {
        let blob = seal(&[1u8; KEY_LEN], &[2u8; NONCE_LEN], "abc");
        assert_eq!(decrypt(&[0u8; KEY_LEN], &blob), None);
    }

    #[test]
    fn rejects_short_or_corrupt_blob() {
        let key = [9u8; KEY_LEN];
        assert_eq!(decrypt(&key, &[0u8; 20]), None);
        let mut blob = seal(&key, &[5u8; NONCE_LEN], "abc");
        let last = blob.len() - 1;
        blob[last] ^= 0xFF;
        assert_eq!(decrypt(&key, &blob), None);
    }

    #[test]
    fn staged_resource_decrypts_when_configured() {
        // 开源仓库里没有加密资源，build.rs 会写占位；此时跳过。
        if KEY.iter().all(|byte| *byte == 0) {
            return;
        }
        let token = builtin_token().expect("资源已配置但解密失败");
        assert!(token.len() >= 16, "解密出的令牌长度异常");
    }
}