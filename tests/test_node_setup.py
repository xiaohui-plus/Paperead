"""node 运行时按需安装的下载 / 校验 / 解压 / 幂等回归。

这里不依赖真实 node.exe：只验证「下载 → SHA-256 校验 → 解压出唯一文件」
这条链路和状态机，运行可用性由 ``_verify``（隐藏窗口跑 ``node --version``）
在真实环境里负责。
"""

from __future__ import annotations

import hashlib
import http.server
import io
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from server import node_setup


def _zip_with_node(name: str = "node.exe", data: bytes = b"fake-node") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr(name, data)
    return buffer.getvalue()


class _Handler(http.server.BaseHTTPRequestHandler):
    payload = b""

    def do_GET(self) -> None:  # noqa: N802 - http.server 约定
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, *args: object) -> None:  # 静默测试日志
        pass


@pytest.fixture()
def server():
    handler = type("Handler", (_Handler,), {"payload": _zip_with_node()})
    httpd = http.server.HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(url=f"http://127.0.0.1:{httpd.server_port}/asset.zip")
    finally:
        httpd.shutdown()


@pytest.fixture()
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("PAPEREAD_DATA", str(tmp_path))
    monkeypatch.delenv("PAPEREAD_MOCK", raising=False)
    monkeypatch.delenv("PAPEREAD_NODE_URL", raising=False)
    node_setup._STATE.update(
        status="idle", percent=0, error="", startedAt=0, finishedAt=0
    )
    node_setup._THREAD = None
    yield tmp_path
    node_setup._THREAD = None


def test_install_extracts_node_after_sha_check(
    monkeypatch: pytest.MonkeyPatch, isolated: Path, server
) -> None:
    payload = _zip_with_node()
    monkeypatch.setattr(
        node_setup, "ASSET_SHA256", hashlib.sha256(payload).hexdigest().upper()
    )
    monkeypatch.setattr(node_setup, "_sources", lambda: [server.url])
    monkeypatch.setattr(node_setup, "_verify", lambda exe: None)
    monkeypatch.setattr(node_setup, "_apply_to_mathjax", lambda: None)

    node_setup._run()

    state = node_setup.status()
    assert state["status"] == "done"
    assert state["installed"] is True
    exe = node_setup.installed_exe()
    assert exe is not None
    assert exe.name == "node.exe"
    # 只解出 node.exe，压缩包本身不留在安装目录。
    assert not (node_setup.install_dir() / node_setup.ASSET_NAME).exists()


def test_bad_sha_marks_error_and_removes_artifact(
    monkeypatch: pytest.MonkeyPatch, isolated: Path, server
) -> None:
    monkeypatch.setattr(node_setup, "ASSET_SHA256", "0" * 64)
    monkeypatch.setattr(node_setup, "_sources", lambda: [server.url])
    monkeypatch.setattr(node_setup, "_verify", lambda exe: None)
    monkeypatch.setattr(node_setup, "_apply_to_mathjax", lambda: None)

    node_setup._run()

    state = node_setup.status()
    assert state["status"] == "error"
    assert state["installed"] is False
    assert "校验失败" in state["error"]
    assert node_setup.installed_exe() is None


def test_archive_without_node_exe_is_rejected(
    monkeypatch: pytest.MonkeyPatch, isolated: Path
) -> None:
    payload = _zip_with_node("readme.txt", b"no binary")

    class Handler(_Handler):
        pass

    Handler.payload = payload
    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setattr(
            node_setup, "ASSET_SHA256", hashlib.sha256(payload).hexdigest().upper()
        )
        monkeypatch.setattr(
            node_setup,
            "_sources",
            lambda: [f"http://127.0.0.1:{httpd.server_port}/asset.zip"],
        )
        monkeypatch.setattr(node_setup, "_apply_to_mathjax", lambda: None)
        node_setup._run()
    finally:
        httpd.shutdown()

    state = node_setup.status()
    assert state["status"] == "error"
    assert "node.exe" in state["error"]
    assert node_setup.installed_exe() is None


def test_start_if_missing_skips_in_mock(
    monkeypatch: pytest.MonkeyPatch, isolated: Path
) -> None:
    monkeypatch.setenv("PAPEREAD_MOCK", "1")
    node_setup.start_if_missing()
    assert node_setup.status()["status"] == "idle"


def test_start_if_missing_registers_existing_node(
    monkeypatch: pytest.MonkeyPatch, isolated: Path
) -> None:
    target = node_setup.install_dir()
    target.mkdir(parents=True, exist_ok=True)
    (target / "node.exe").write_bytes(b"installed")
    registered: list[str] = []
    monkeypatch.setattr(node_setup, "_apply_to_mathjax", lambda: registered.append("hit"))

    node_setup.start_if_missing()

    assert registered == ["hit"]
    assert node_setup.status()["status"] == "done"


def test_set_node_path_registers_and_resets_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import os

    from paperead import mathjax_render

    exe = tmp_path / "node.exe"
    exe.write_bytes(b"")

    mathjax_render._AVAILABLE = False
    try:
        mathjax_render.set_node_path(str(exe))
        assert os.environ["PAPEREAD_NODE"] == str(exe)
        assert mathjax_render._AVAILABLE is None
    finally:
        # 同一进程里后面的公式测试还要用真实 node，必须把缓存还原。
        os.environ.pop("PAPEREAD_NODE", None)
        mathjax_render._NODE_EXE = mathjax_render._UNSET
        mathjax_render._AVAILABLE = None
        mathjax_render._AVAILABLE_AT = 0.0