use std::time::Duration;

use serde_json::{json, Value};

pub fn health(port: u16, token: &str) -> Result<Value, String> {
    let url = format!("http://127.0.0.1:{port}/api/health");
    let response = ureq::get(&url)
        .set("X-Paper-Reflow-Token", token)
        .timeout(Duration::from_millis(800))
        .call()
        .map_err(|error| error.to_string())?;
    response
        .into_json::<Value>()
        .map_err(|error| error.to_string())
}

pub fn import_pdf(port: u16, token: &str, path: &str) -> Result<String, String> {
    let url = format!("http://127.0.0.1:{port}/api/papers/import");
    let response = ureq::post(&url)
        .set("X-Paper-Reflow-Token", token)
        .timeout(Duration::from_secs(120))
        .send_json(json!({ "path": path }))
        .map_err(|error| error.to_string())?;
    let value: Value = response
        .into_json()
        .map_err(|error| error.to_string())?;
    value["added"][0]["id"]
        .as_str()
        .map(str::to_owned)
        .ok_or_else(|| format!("导入响应异常：{value}"))
}
