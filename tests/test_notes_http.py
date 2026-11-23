"""笔记取数层的 curl 子进程必须隐藏控制台窗口。

sidecar 本身没有控制台，裸 ``subprocess.run(["curl", ...])`` 会在桌面端每个
请求弹出一个 cmd 窗口；笔记广场一次会并发拉多篇正文与摘要，表现为窗口连续
弹出。这里守住「所有 curl 调用都带隐藏窗口参数」。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import httpx
import pytest

import server.notes_http as notes_http
import server.winproc as winproc


class _StubStream:
    def __init__(self) -> None:
        raise httpx.ConnectError("boom")

    def __enter__(self):  # pragma: no cover - 构造即抛错
        raise AssertionError("unreachable")

    def __exit__(self, *args):  # pragma: no cover
        return False


class _StubClient:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self) -> "_StubClient":
        return self

    def __exit__(self, *args) -> bool:
        return False

    def stream(self, *args, **kwargs) -> _StubStream:
        return _StubStream()


def _assert_hidden(kwargs: dict) -> None:
    if os.name != "nt":
        return
    assert kwargs.get("creationflags") == subprocess.CREATE_NO_WINDOW
    startupinfo = kwargs.get("startupinfo")
    assert startupinfo is not None
    assert startupinfo.dwFlags & subprocess.STARTF_USESHOWWINDOW
    assert startupinfo.wShowWindow == subprocess.SW_HIDE


def test_curl_uses_hidden_window(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict] = []

    def fake_run(command, **kwargs):
        captured.append(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout=b"ok", stderr=b"")

    monkeypatch.setattr(winproc.subprocess, "run", fake_run)

    assert notes_http._curl("https://example.invalid/a.md", 1) == b"ok"
    assert captured
    _assert_hidden(captured[-1])


def test_prefix_fallback_uses_hidden_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(notes_http.httpx, "Client", _StubClient)
    captured: list[dict] = []

    def fake_run(command, **kwargs):
        captured.append(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout=b"prefix-bytes", stderr=b"")

    monkeypatch.setattr(winproc.subprocess, "run", fake_run)

    assert notes_http.http_get_prefix("https://example.invalid/b.md", limit=6) == "prefix"
    assert captured
    _assert_hidden(captured[-1])


def test_download_fallback_uses_hidden_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(notes_http.httpx, "Client", _StubClient)
    captured: list[dict] = []

    def fake_run(command, **kwargs):
        captured.append(kwargs)
        dest = Path(command[command.index("-o") + 1])
        dest.write_bytes(b"tar-bytes")
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(winproc.subprocess, "run", fake_run)

    dest = tmp_path / "CVPR2026.tar.gz"
    assert notes_http.http_download("https://example.invalid/c.tar.gz", dest) == 9
    assert dest.read_bytes() == b"tar-bytes"
    assert captured
    _assert_hidden(captured[-1])

def test_prefix_many_uses_one_hidden_curl(monkeypatch: pytest.MonkeyPatch) -> None:
    """整页摘要只起一个 curl 进程，不再每篇各起一个。"""
    calls: list[tuple[list[str], dict]] = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        for index, arg in enumerate(command):
            if arg == "-o":
                Path(command[index + 1]).write_bytes(b"prefix-body")
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(winproc.subprocess, "run", fake_run)

    texts = notes_http.http_get_prefix_many(
        ["https://example.invalid/a.md", "https://example.invalid/b.md"]
    )

    assert len(calls) == 1
    assert texts == ["prefix-body", "prefix-body"]
    assert "--parallel" in calls[0][0]
    _assert_hidden(calls[0][1])
