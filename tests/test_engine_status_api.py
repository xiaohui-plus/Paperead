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

def test_toolchain_exposes_tex_download(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """设置页依赖 ``texDownload`` 展示 LaTeX 自动安装进度与错误。"""
    monkeypatch.setenv("PAPEREAD_TOKEN", "tex-token")
    monkeypatch.setenv("PAPEREAD_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("PAPEREAD_MOCK", "1")

    import server.main as main

    importlib.reload(main)
    client = TestClient(main.app)

    res = client.get(
        "/api/toolchain", headers={"X-Paperead-Token": "tex-token"}
    )

    assert res.status_code == 200
    tex = res.json()["texDownload"]
    assert tex["status"] in {"idle", "running", "done", "error"}
    assert "percent" in tex
    assert "installed" in tex

def test_toolchain_route_ok_when_cache_missing_but_pandoc_downloaded(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """首装后 pandoc 已下载、自检缓存还是空的：不能因 data=None 抛 500。

    旧逻辑 ``stale_pandoc = download.installed and not data.get(...)`` 在
    ``data`` 为 None 时直接 AttributeError，用户点「检查依赖」就只看到 HTTP 500。
    """
    monkeypatch.setenv("PAPEREAD_TOKEN", "deps-token")
    monkeypatch.setenv("PAPEREAD_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("PAPEREAD_MOCK", "1")

    from server import toolchain_api
    from server.research import pandoc_setup

    toolchain_api._CACHE = None
    exe = pandoc_setup.install_dir() / "pandoc.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"stub")

    import server.main as main

    importlib.reload(main)
    client = TestClient(main.app)
    try:
        res = client.get("/api/toolchain", headers={"X-Paperead-Token": "deps-token"})
    finally:
        toolchain_api._CACHE = None

    assert res.status_code == 200
    body = res.json()
    assert body["pandocDownload"]["installed"] is True
    assert "engineDownload" in body


def test_safe_status_degrades_single_component() -> None:
    """单个依赖探测异常只降级该组件，不让整页 500。"""
    from server import toolchain_api

    def boom() -> dict:
        raise RuntimeError("probe failed")

    data = toolchain_api._safe_status(boom, toolchain_api._download_default())

    assert data["status"] == "idle"
    assert data["installed"] is False
    assert "probe failed" in data["error"]