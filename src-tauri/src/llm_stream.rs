//! 手机端直连大模型。
//!
//! WebView 里 `fetch` 调厂商接口会被 CORS 拦（对方不返回 `Access-Control-*`），
//! 所以流式请求放在 Rust 里发，再把 SSE 增量通过 Tauri Channel 逐段推给前端，
//! 保持和服务端 `/api/papers/{id}/ask/stream` 一致的逐字体验。

use std::collections::HashSet;
use std::sync::Mutex;

use futures_util::StreamExt;
use serde::{Deserialize, Serialize};
use tauri::ipc::Channel;
use tauri::State;

#[derive(Debug, Deserialize, Serialize)]
pub struct LlmMessage {
    pub role: String,
    pub content: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct LlmRequest {
    /// 前端生成的请求 id，用于中止。
    pub id: String,
    pub base_url: String,
    pub api_key: String,
    pub model: String,
    pub messages: Vec<LlmMessage>,
    pub temperature: Option<f32>,
    pub max_tokens: Option<u32>,
}

#[derive(Clone, Serialize)]
#[serde(tag = "type", rename_all = "camelCase")]
pub enum LlmEvent {
    Delta { text: String },
    Done,
    Error { message: String },
}

/// 正在进行的请求 id；前端点「停止」时插入，流循环下次迭代就退出。
#[derive(Default)]
pub struct Cancels(Mutex<HashSet<String>>);

#[tauri::command]
pub async fn llm_cancel(id: String, cancels: State<'_, Cancels>) -> Result<(), String> {
    let mut set = cancels.0.lock().map_err(|error| error.to_string())?;
    set.insert(id);
    Ok(())
}

fn is_cancelled(cancels: &Cancels, id: &str) -> bool {
    cancels
        .0
        .lock()
        .map(|set| set.contains(id))
        .unwrap_or(false)
}

/// 请求结束时清掉占位，避免取消集合无限增长。
struct CancelGuard<'a> {
    cancels: &'a Cancels,
    id: String,
}

impl Drop for CancelGuard<'_> {
    fn drop(&mut self) {
        if let Ok(mut set) = self.cancels.0.lock() {
            set.remove(&self.id);
        }
    }
}

/// 与 `paperead/llm.py` 保持一致：这些模型的 temperature 固定，显式传会报错。
fn fixed_temperature_model(model: &str) -> bool {
    let name = model.to_lowercase();
    name.starts_with("kimi-k3") || name.starts_with("kimi-k2.5")
}

fn endpoint(base_url: &str) -> String {
    let base = base_url.trim().trim_end_matches('/');
    if base.ends_with("/chat/completions") {
        base.to_string()
    } else {
        format!("{base}/chat/completions")
    }
}

#[tauri::command]
pub async fn llm_stream(
    request: LlmRequest,
    on_event: Channel<LlmEvent>,
    cancels: State<'_, Cancels>,
) -> Result<(), String> {
    if request.model.trim().is_empty() {
        return Err("模型名不能为空".into());
    }
    let _guard = CancelGuard {
        cancels: &cancels,
        id: request.id.clone(),
    };

    let mut body = serde_json::json!({
        "model": request.model,
        "messages": request.messages,
        "stream": true,
    });
    if !fixed_temperature_model(&request.model) {
        body["temperature"] = serde_json::json!(request.temperature.unwrap_or(0.25));
    }
    if let Some(max_tokens) = request.max_tokens {
        body["max_tokens"] = serde_json::json!(max_tokens);
    }

    let client = reqwest::Client::builder()
        .connect_timeout(std::time::Duration::from_secs(20))
        .build()
        .map_err(|error| format!("初始化网络失败：{error}"))?;

    let mut builder = client
        .post(endpoint(&request.base_url))
        .header("Content-Type", "application/json")
        .header("Accept", "text/event-stream")
        .json(&body);
    if !request.api_key.trim().is_empty() {
        builder = builder.header("Authorization", format!("Bearer {}", request.api_key.trim()));
    }

    let response = builder.send().await.map_err(|error| {
        let message = if error.is_connect() {
            format!("无法连接模型服务：{error}")
        } else if error.is_timeout() {
            "连接模型服务超时".to_string()
        } else {
            format!("请求模型失败：{error}")
        };
        let _ = on_event.send(LlmEvent::Error {
            message: message.clone(),
        });
        message
    })?;

    if !response.status().is_success() {
        let status = response.status().as_u16();
        let detail = response.text().await.unwrap_or_default();
        let message = format!("模型返回 HTTP {status}：{}", detail.trim().chars().take(300).collect::<String>());
        let _ = on_event.send(LlmEvent::Error {
            message: message.clone(),
        });
        return Err(message);
    }

    let mut stream = response.bytes_stream();
    let mut buffer: Vec<u8> = Vec::with_capacity(8192);

    while let Some(chunk) = stream.next().await {
        if is_cancelled(&cancels, &request.id) {
            break;
        }
        let chunk = match chunk {
            Ok(bytes) => bytes,
            Err(error) => {
                let message = format!("读取模型流失败：{error}");
                let _ = on_event.send(LlmEvent::Error {
                    message: message.clone(),
                });
                return Err(message);
            }
        };
        buffer.extend_from_slice(&chunk);

        // SSE 以换行分帧；多字节 UTF-8 字符不会包含 0x0A，按行切分是安全的。
        while let Some(pos) = buffer.iter().position(|byte| *byte == b'\n') {
            let line: Vec<u8> = buffer.drain(..=pos).collect();
            let text = String::from_utf8_lossy(&line);
            let text = text.trim();
            let Some(payload) = text.strip_prefix("data:") else {
                continue;
            };
            let payload = payload.trim();
            if payload.is_empty() {
                continue;
            }
            if payload == "[DONE]" {
                let _ = on_event.send(LlmEvent::Done);
                return Ok(());
            }
            let Ok(value) = serde_json::from_str::<serde_json::Value>(payload) else {
                continue;
            };
            if let Some(message) = value.get("error").and_then(|error| error.get("message")) {
                let message = format!("模型返回错误：{message}");
                let _ = on_event.send(LlmEvent::Error {
                    message: message.clone(),
                });
                return Err(message);
            }
            if let Some(delta) = value
                .get("choices")
                .and_then(|choices| choices.get(0))
                .and_then(|choice| choice.get("delta"))
                .and_then(|delta| delta.get("content"))
                .and_then(|content| content.as_str())
            {
                if !delta.is_empty() && on_event.send(LlmEvent::Delta { text: delta.to_string() }).is_err() {
                    // 前端已经销毁（切走页面），停止拉流。
                    return Ok(());
                }
            }
        }
    }

    let _ = on_event.send(LlmEvent::Done);
    Ok(())
}
