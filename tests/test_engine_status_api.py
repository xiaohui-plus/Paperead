from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


def test_status_reports_ready_separately(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """``installed`` 表示任意版本可用，``ready`` 才是当前版本就位。

    设置页与翻译入口按 ready 判断；否则旧引擎目录存在会误报「已就绪」。
    """
    monkeypatch.delenv("PAPEREAD_BABELDOC_HOME", raising=False)
    monkeypatch.setenv("PAPEREAD_DATA", str(tmp_path / "data"))
    from server import babeldoc_setup

    data = babeldoc_setup.status()

    assert "ready" in data
    assert data["ready"] is False


def test_toolchain_status_includes_engine(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """写作工具自检必须带上全局翻译引擎，避免只显示 PDF 导出链的「已就绪」。"""
    monkeypatch.delenv("PAPEREAD_BABELDOC_HOME", raising=False)
    monkeypatch.setenv("PAPEREAD_DATA", str(tmp_path / "data"))
    from server import toolchain_api

    data = toolchain_api.status(refresh=True)

    assert "engineDownload" in data
    assert "ready" in data["engineDownload"]
    assert data["engineDownload"]["version"]


def test_engine_route_registered(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """``GET /api/toolchain/engine`` 必须真实可访问（include_router 生效）。"""
    monkeypatch.setenv("PAPEREAD_TOKEN", "engine-token")
    monkeypatch.setenv("PAPEREAD_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("PAPEREAD_MOCK", "1")

    import server.main as main

    importlib.reload(main)
    # 不进 with：不触发 lifespan，避免测试环境启动任何后台下载。
    client = TestClient(main.app)

    res = client.get(
        "/api/toolchain/engine", headers={"X-Paperead-Token": "engine-token"}
    )

    assert res.status_code == 200
    body = res.json()
    assert body["ready"] is False
    assert body["version"]