"""设置持久化与 agent 模型同步的回归测试。

覆盖两条硬要求：
- 配置写到数据目录，换端口 / 升级后仍能从后端读回（不依赖 localStorage）；
- 设置页保存的回答模型会被研究 agent 的本地代理直接使用，不需要再配一遍。
"""

import gzip
import importlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fastapi.testclient import TestClient


def _prepare(monkeypatch, tmp_path: Path) -> Path:
    data = tmp_path / "lib"
    data.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PAPEREAD_DATA", str(data))
    monkeypatch.setenv("PAPEREAD_MOCK", "1")
    monkeypatch.delenv("PAPEREAD_TOKEN", raising=False)
    for name in ("PAPEREAD_LLM_KEY", "PAPEREAD_LLM_BASE", "PAPEREAD_MODEL"):
        monkeypatch.delenv(name, raising=False)
    return data


def test_app_settings_round_trip(monkeypatch, tmp_path: Path):
    data = _prepare(monkeypatch, tmp_path)
    import server.main as main

    importlib.reload(main)
    with TestClient(main.app) as client:
        saved = client.put(
            "/api/settings/app",
            json={
                "settings": {
                    "paperead.readingMode": "zh",
                    "paperead.readingFonts": '["serif"]',
                    "unknown.key": "should-drop",
                }
            },
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["keys"] == 2

        payload = client.get("/api/settings/app").json()
        assert payload["settings"]["paperead.readingMode"] == "zh"
        assert "unknown.key" not in payload["settings"]
    assert (data / "app-settings.json").is_file()


def test_agent_proxy_reuses_saved_answer_model(monkeypatch, tmp_path: Path):
    data = _prepare(monkeypatch, tmp_path)
    # 只写设置页那份配置，模拟「研究模型选择没单独同步过」
    (data / "model-settings.json").write_text(
        json.dumps(
            {
                "settings": {
                    "models": [
                        {
                            "id": "m1",
                            "kind": "answer",
                            "label": "DeepSeek",
                            "baseUrl": "https://api.example.com/v1",
                            "apiKey": "sk-1234567890abcdef",
                            "model": "deepseek-chat",
                        }
                    ],
                    "active": {"answer": "m1"},
                }
            }
        ),
        encoding="utf-8",
    )
    import server.main as main

    importlib.reload(main)
    with TestClient(main.app) as client:
        status = client.get("/api/research/model").json()
    assert status["configured"] is True
    assert status["base_url"] == "https://api.example.com/v1"
    assert status["model"] == "deepseek-chat"
    assert status["api_key"] != "sk-1234567890abcdef"
    assert (data / "research-model.json").is_file()


def _settings_with_model(data: Path) -> None:
    (data / "model-settings.json").write_text(
        json.dumps(
            {
                "settings": {
                    "models": [
                        {
                            "id": "m1",
                            "kind": "answer",
                            "label": "旧服务商",
                            "baseUrl": "https://api.example.com/v1",
                            "apiKey": "sk-1234567890abcdef",
                            "model": "deepseek-chat",
                        }
                    ],
                    "active": {"answer": "m1"},
                }
            }
        ),
        encoding="utf-8",
    )


def test_research_model_choice_wins_over_settings_file(monkeypatch, tmp_path: Path):
    """研究页选过的模型不能被设置页里的旧条目覆盖。

    阅读问答（localStorage）和研究 agent（数据目录里的文件）一旦指向不同服务商，
    用户会看到「阅读 AI 能用、研究 agent 一直失败」，所以这里锁死优先级。
    """
    data = _prepare(monkeypatch, tmp_path)
    _settings_with_model(data)
    import server.main as main

    importlib.reload(main)
    with TestClient(main.app) as client:
        client.put(
            "/api/research/model",
            json={
                "base_url": "https://api.chosen.com/v1",
                "api_key": "sk-chosen-key-123456",
                "model": "chosen-model",
                "label": "chosen",
            },
        )
        status = client.get("/api/research/model").json()
        assert status["base_url"] == "https://api.chosen.com/v1"
        assert status["model"] == "chosen-model"

    saved = json.loads((data / "research-model.json").read_text(encoding="utf-8"))
    assert saved["model"] == "chosen-model"

    # 重启后端后也保持研究页的选择，不再回退到设置页的旧条目。
    importlib.reload(main)
    with TestClient(main.app) as client:
        status = client.get("/api/research/model").json()
    assert status["model"] == "chosen-model"


class _FakeModel(BaseHTTPRequestHandler):
    """假装成一个 OpenAI 兼容服务；响应由测试通过 `reply` 注入。"""

    reply: tuple[str, bytes] = ("application/json", b"{}")
    extra_headers: dict[str, str] = {}
    requests: list[dict[str, str]] = []

    def do_POST(self) -> None:  # noqa: N802 - http.server 的固定接口名
        length = int(self.headers.get("content-length") or 0)
        self.rfile.read(length)
        type(self).requests.append(dict(self.headers))
        content_type, body = type(self).reply
        self.send_response(200)
        self.send_header("content-type", content_type)
        for name, value in type(self).extra_headers.items():
            self.send_header(name, value)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


def _serve_model(
    reply: tuple[str, bytes], headers: dict[str, str] | None = None
) -> ThreadingHTTPServer:
    _FakeModel.reply = reply
    _FakeModel.extra_headers = dict(headers or {})
    _FakeModel.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeModel)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _point_at(monkeypatch, client: TestClient, server: ThreadingHTTPServer) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    client.put(
        "/api/research/model",
        json={
            "base_url": f"http://127.0.0.1:{server.server_address[1]}/v1",
            "api_key": "sk-local-test-123456",
            "model": "fake-model",
            "label": "fake",
        },
    )


def _call_agent(client: TestClient):
    return client.post(
        "/api/llm-proxy/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "你好"}],
            "stream": True,
        },
    )


def test_agent_proxy_wraps_non_streaming_completion(monkeypatch, tmp_path: Path):
    """供应商忽略 stream:true 时，代理要把完整结果改写成 SSE，agent 才不会 0 秒失败。"""
    _prepare(monkeypatch, tmp_path)
    payload = {
        "id": "cmpl-1",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "这是模型回答。"},
                "finish_reason": "stop",
            }
        ],
    }
    server = _serve_model(
        ("application/json", json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    )
    try:
        import server.main as main

        importlib.reload(main)
        with TestClient(main.app) as client:
            _point_at(monkeypatch, client, server)
            response = _call_agent(client)
    finally:
        server.shutdown()
        server.server_close()

    assert response.status_code == 200, response.text
    assert "text/event-stream" in response.headers["content-type"]
    assert "data: [DONE]" in response.text
    assert "这是模型回答。" in response.text


def test_agent_proxy_passes_sse_through(monkeypatch, tmp_path: Path):
    """正常供应商的 SSE 要原样透传，不能被兜底逻辑改写。"""
    _prepare(monkeypatch, tmp_path)
    sse = (
        b'data: {"choices":[{"index":0,"delta":{"content":"\xe5\x97\xa8"},"finish_reason":null}]}\n\n'
        b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
        b"data: [DONE]\n\n"
    )
    server = _serve_model(("text/event-stream", sse))
    try:
        import server.main as main

        importlib.reload(main)
        with TestClient(main.app) as client:
            _point_at(monkeypatch, client, server)
            response = _call_agent(client)
    finally:
        server.shutdown()
        server.server_close()

    assert response.status_code == 200, response.text
    assert "text/event-stream" in response.headers["content-type"]
    assert response.content == sse


def test_agent_proxy_adds_done_when_upstream_omits_it(monkeypatch, tmp_path: Path):
    """网关只发数据块、不发 [DONE] 时，代理要补齐，否则 agent 整轮 STREAM_CLOSED。"""
    _prepare(monkeypatch, tmp_path)
    sse = (
        b'data: {"choices":[{"index":0,"delta":{"content":"\xe5\x97\xa8"},"finish_reason":null}]}\n\n'
        b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    )
    server = _serve_model(("text/event-stream", sse))
    try:
        import server.main as main

        importlib.reload(main)
        with TestClient(main.app) as client:
            _point_at(monkeypatch, client, server)
            response = _call_agent(client)
    finally:
        server.shutdown()
        server.server_close()

    assert response.status_code == 200, response.text
    assert response.content.startswith(sse)
    assert response.content.endswith(b"data: [DONE]\n\n")
    assert response.content.count(b"[DONE]") == 1
    assert _FakeModel.requests[0].get("accept-encoding") == "identity"


def test_agent_proxy_decodes_compressed_sse(monkeypatch, tmp_path: Path):
    """上游压缩的 SSE 必须解码后再转发，否则 agent 解析不到事件、报 STREAM_CLOSED。"""
    _prepare(monkeypatch, tmp_path)
    plain = (
        b'data: {"choices":[{"index":0,"delta":{"content":"\xe5\x97\xa8"},"finish_reason":null}]}\n\n'
        b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    )
    server = _serve_model(
        ("text/event-stream", gzip.compress(plain)),
        headers={"content-encoding": "gzip"},
    )
    try:
        import server.main as main

        importlib.reload(main)
        with TestClient(main.app) as client:
            _point_at(monkeypatch, client, server)
            response = _call_agent(client)
    finally:
        server.shutdown()
        server.server_close()

    assert response.status_code == 200, response.text
    assert response.content.startswith(plain)
    assert b"\x1f\x8b" not in response.content
    assert response.content.endswith(b"data: [DONE]\n\n")


def test_agent_proxy_reports_non_api_body(monkeypatch, tmp_path: Path):
    """地址填成了网页时给出可读错误，而不是让 agent 报 STREAM_CLOSED。"""
    _prepare(monkeypatch, tmp_path)
    server = _serve_model(
        ("text/html", b"<!DOCTYPE html><html><body>Sign in</body></html>")
    )
    try:
        import server.main as main

        importlib.reload(main)
        with TestClient(main.app) as client:
            _point_at(monkeypatch, client, server)
            response = _call_agent(client)
    finally:
        server.shutdown()
        server.server_close()

    assert response.status_code == 502
    message = response.json()["error"]["message"]
    assert "不是 API 响应" in message
    assert "/v1" in message
