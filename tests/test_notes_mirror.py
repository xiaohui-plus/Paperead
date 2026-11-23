"""笔记镜像：按会议解压、路径安全与读取优先级。"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

import server.notes_api as notes_api
import server.notes_mirror as notes_mirror
from server.notes_mirror import NotesMirror, _sha256

MD = """---
title: Demo Paper
description: 一句话摘要。
tags:
  - "CVPR 2026"
---

# Demo Paper

## 一句话总结
演示用。
""".encode("utf-8")


def _make_tar(path: Path, members: list[tuple[str, bytes]]) -> Path:
    with tarfile.open(path, "w:gz") as tar:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


def _archive(tmp_path: Path) -> Path:
    return _make_tar(
        tmp_path / "CVPR2026.tar.gz",
        [
            ("paper-notes-main/docs/CVPR2026/llm_reasoning/a.md", MD),
            ("paper-notes-main/docs/ICLR2026/llm_agent/b.md", MD),
        ],
    )


def test_extract_only_requested_conference(tmp_path: Path) -> None:
    mirror = NotesMirror(tmp_path / "data")

    result = mirror.extract(_archive(tmp_path), "CVPR2026")

    assert result["conferences"]["CVPR2026"]["notes"] == 1
    assert result["conferences"]["CVPR2026"]["bytes"] == len(MD)
    assert mirror.note_path("CVPR2026/llm_reasoning/a.md") is not None
    assert mirror.note_path("ICLR2026/llm_agent/b.md") is None


def test_extract_rejects_path_traversal(tmp_path: Path) -> None:
    archive = _make_tar(
        tmp_path / "bad.tar.gz",
        [("docs/CVPR2026/../../evil.md", b"x")],
    )
    mirror = NotesMirror(tmp_path / "data")

    mirror.extract(archive, "CVPR2026")

    assert not (tmp_path / "data" / "notes-mirror" / "evil.md").exists()
    assert not (tmp_path / "evil.md").exists()


def test_note_path_rejects_unsafe_relative(tmp_path: Path) -> None:
    mirror = NotesMirror(tmp_path / "data")

    assert mirror.note_path("../etc/passwd") is None
    assert mirror.note_path("/abs/path.md") is None


def test_online_prefers_mirror_over_network(tmp_path: Path, monkeypatch) -> None:
    mirror = NotesMirror(tmp_path / "data")
    mirror.extract(_archive(tmp_path), "CVPR2026")

    def offline(*args, **kwargs):
        raise RuntimeError("不应访问网络")

    monkeypatch.setattr(notes_api, "http_get_text_any", offline)

    note = notes_api._parse_online("docs/CVPR2026/llm_reasoning/a.md", mirror)

    assert note.title == "Demo Paper"
    assert note.source_path == "CVPR2026/llm_reasoning/a.md"
    assert note.blocks


def _manifest_for(archive: Path, sha: str | None = None) -> str:
    return json.dumps(
        {
            "version": "v2",
            "conferences": [
                {
                    "conference": "CVPR2026",
                    "sha256": sha or _sha256(archive),
                    "bytes": archive.stat().st_size,
                    "url": "https://example.invalid/CVPR2026.tar.gz",
                }
            ],
        },
        ensure_ascii=False,
    )


def _run(mirror: NotesMirror) -> dict:
    mirror.auto_sync("https://manifest")
    if mirror._sync_thread is not None:
        mirror._sync_thread.join(10)
    return mirror.sync_status()


def test_auto_sync_skips_matching_hash(tmp_path: Path, monkeypatch) -> None:
    archive = _archive(tmp_path)
    mirror = NotesMirror(tmp_path / "data")
    mirror.extract(archive, "CVPR2026")
    mirror._state["conferences"]["CVPR2026"] = {"sha256": _sha256(archive)}
    monkeypatch.setattr(
        notes_mirror, "http_get_text", lambda *a, **k: _manifest_for(archive)
    )
    monkeypatch.setattr(
        notes_mirror, "http_download", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应下载")),
    )

    status = _run(mirror)
    assert "CVPR2026" in status["skipped"], repr(status.get("error"))
    assert status["updated"] == []
    assert status["state"] == "done"


def test_auto_sync_replaces_when_hash_changes(tmp_path: Path, monkeypatch) -> None:
    old = _archive(tmp_path)
    mirror = NotesMirror(tmp_path / "data")
    mirror.extract(old, "CVPR2026")
    mirror._state["conferences"]["CVPR2026"] = {"sha256": _sha256(old)}

    newer = _make_tar(
        tmp_path / "CVPR2026.new.tar.gz",
        [("docs/CVPR2026/llm_reasoning/new.md", MD)],
    )
    monkeypatch.setattr(
        notes_mirror, "http_get_text", lambda *a, **k: _manifest_for(newer)
    )

    def fake_download(url, dest, timeout=0):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(newer.read_bytes())
        return dest.stat().st_size

    monkeypatch.setattr(notes_mirror, "http_download", fake_download)

    status = _run(mirror)

    assert "CVPR2026" in status["updated"]
    assert mirror.note_path("CVPR2026/llm_reasoning/new.md") is not None
    # 旧文件必须被整体替换掉，而不是与新文件混在一起
    assert mirror.note_path("CVPR2026/llm_reasoning/a.md") is None


def test_auto_sync_reports_hash_mismatch(tmp_path: Path, monkeypatch) -> None:
    archive = _archive(tmp_path)
    mirror = NotesMirror(tmp_path / "data")
    mirror._state["conferences"]["CVPR2026"] = {"sha256": "stale"}
    monkeypatch.setattr(
        notes_mirror, "http_get_text", lambda *a, **k: _manifest_for(archive, sha="0" * 64),
    )

    def fake_download(url, dest, timeout=0):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(archive.read_bytes())
        return dest.stat().st_size

    monkeypatch.setattr(notes_mirror, "http_download", fake_download)

    status = _run(mirror)

    assert status["failed"] and status["failed"][0][0] == "CVPR2026"
    assert "CVPR2026" not in status["updated"]


def test_auto_sync_migrates_existing_mirror_without_state(
    tmp_path: Path, monkeypatch
) -> None:
    archive = _archive(tmp_path)
    mirror = NotesMirror(tmp_path / "data")
    mirror.extract(archive, "CVPR2026")  # 有镜像但没有 state 记录

    monkeypatch.setattr(
        notes_mirror, "http_get_text", lambda *a, **k: _manifest_for(archive)
    )
    monkeypatch.setattr(
        notes_mirror, "http_download", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应下载")),
    )

    status = mirror.auto_sync("https://manifest")

    assert "CVPR2026" in status["skipped"]
    assert status["updated"] == []
    assert mirror._state["conferences"]["CVPR2026"]["sha256"] == _sha256(archive)
