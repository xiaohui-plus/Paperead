"""划词翻译：文本翻译函数与 /api/translate/text 接口。"""

from __future__ import annotations

import asyncio
import importlib

import pytest
from fastapi.testclient import TestClient

from paperead import llm as llm_mod
from paperead.translate import translate_text

CONFIG = {
    "base_url": "https://api.example.com/v1",
    "api_key": "test-key-123456",
    "model": "test-model",
}


class EchoBackend:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[tuple[list[dict], dict]] = []

    async def chat(self, messages, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(([dict(m) for m in messages], kwargs))
        return self.reply


def test_translate_text_restores_placeholders() -> None:
    """引用编号先被占位符保护，模型返回后必须原样还原。"""
    backend = EchoBackend("注意力机制见 ⟦P0⟧。")
    llm_mod.set_backend(backend)
    try:
        out = asyncio.run(translate_text("See attention in [12].", target_name="中文"))
    finally:
        llm_mod.set_backend(None)

    assert out == "注意力机制见 [12]。"
    system, user = backend.calls[0][0]
    assert system["role"] == "system"
    assert "⟦P0⟧" in user["content"]
    assert "[12]" not in user["content"]
    assert backend.calls[0][1]["max_tokens"] >= 512


def test_translate_text_rejects_blank() -> None:
    with pytest.raises(ValueError):
        asyncio.run(translate_text("   ", target_name="中文"))


def _load_main(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPEREAD_DATA", str(tmp_path / "lib"))
    monkeypatch.setenv("PAPEREAD_MOCK", "1")
    monkeypatch.delenv("PAPEREAD_TOKEN", raising=False)

    import server.main as main

    importlib.reload(main)
    return main


def test_translate_text_route(monkeypatch, tmp_path) -> None:
    main = _load_main(monkeypatch, tmp_path)
    seen: dict[str, str] = {}

    async def fake_translate(text: str, target_name: str = "中文", **kwargs) -> str:
        seen["text"] = text
        seen["target"] = target_name
        return "译好的句子"

    monkeypatch.setattr(main, "translate_text", fake_translate)
    client = TestClient(main.app)

    res = client.post(
        "/api/translate/text",
        json={"text": "  Hello world.  ", "config": CONFIG, "target_lang": "ja"},
    )

    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["translation"] == "译好的句子"
    assert body["target_lang"] == "ja"
    assert body["target_name"] == "日语"
    assert seen == {"text": "Hello world.", "target": "日语"}


def test_translate_text_route_blank(monkeypatch, tmp_path) -> None:
    main = _load_main(monkeypatch, tmp_path)
    client = TestClient(main.app)

    res = client.post(
        "/api/translate/text",
        json={"text": "   ", "config": CONFIG, "target_lang": "zh-Hans"},
    )

    assert res.status_code == 400
    assert "没有可翻译的文字" in res.json()["detail"]


def test_translate_text_route_reports_model_error(monkeypatch, tmp_path) -> None:
    main = _load_main(monkeypatch, tmp_path)

    async def boom(text: str, target_name: str = "中文", **kwargs) -> str:
        raise RuntimeError("模型超时")

    monkeypatch.setattr(main, "translate_text", boom)
    client = TestClient(main.app)

    res = client.post(
        "/api/translate/text",
        json={"text": "Hello", "config": CONFIG, "target_lang": "zh-Hans"},
    )

    assert res.status_code == 502
    assert "翻译失败" in res.json()["detail"]
    assert "模型超时" in res.json()["detail"]