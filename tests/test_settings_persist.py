"""设置持久化与 agent 模型同步的回归测试。

覆盖两条硬要求：
- 配置写到数据目录，换端口 / 升级后仍能从后端读回（不依赖 localStorage）；
- 设置页保存的回答模型会被研究 agent 的本地代理直接使用，不需要再配一遍。
"""

import importlib
import json
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
