from __future__ import annotations

import time
from pathlib import Path

import pytest

from server import storage


def _make_library(root: Path) -> None:
    (root / "papers").mkdir(parents=True)
    (root / "papers" / "a.pdf").write_bytes(b"a" * 1024)
    (root / "papers" / "b.pdf").write_bytes(b"b" * 4096)
    (root / "research").mkdir()
    (root / "research" / "research.db").write_bytes(b"")
    (root / "research" / "note.md").write_text("hi", encoding="utf-8")
    (root / "library.json").write_text("[]", encoding="utf-8")
    for skipped in ("logs", "tmp", "thumb-cache", "tools"):
        (root / skipped).mkdir()
        (root / skipped / "junk.bin").write_bytes(b"x" * 100)
    (root / "cache").mkdir()
    (root / "cache" / "data.bin").write_bytes(b"c" * 512)
    (root / "stale.tmp").write_text("tmp", encoding="utf-8")


def test_relocate_copies_files_and_skips_work_dirs(tmp_path: Path) -> None:
    source = tmp_path / "old"
    target = tmp_path / "new"
    _make_library(source)

    result = storage.relocate(source, target)

    assert result["ok"] is True
    assert result["files"] == 6
    assert (target / "papers" / "a.pdf").read_bytes() == b"a" * 1024
    assert (target / "research" / "research.db").is_file()
    assert (target / "library.json").is_file()
    assert (target / "cache" / "data.bin").is_file()
    for skipped in ("logs", "tmp", "thumb-cache", "tools"):
        assert not (target / skipped).exists()
    assert not (target / "stale.tmp").exists()
    assert source.is_dir()


def test_relocate_reports_progress(tmp_path: Path) -> None:
    source = tmp_path / "old"
    target = tmp_path / "new"
    _make_library(source)
    progress = storage.RelocationProgress(target)

    storage.relocate(source, target, progress)
    progress.finish({"ok": True})
    snapshot = progress.snapshot()

    assert snapshot["phase"] == "done"
    assert snapshot["files_total"] == 6
    assert snapshot["files_done"] == 6
    assert snapshot["bytes_total"] == 1024 + 4096 + 0 + 2 + 2 + 512
    assert snapshot["bytes_done"] >= snapshot["bytes_total"]
    assert snapshot["error"] == ""


def test_background_job_reaches_done(tmp_path: Path) -> None:
    source = tmp_path / "old"
    target = tmp_path / "new"
    _make_library(source)
    done: list[dict] = []

    job = storage.start_relocate(source, target, on_success=done.append)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        snapshot = storage.get_job(job.id)
        assert snapshot is not None
        if snapshot["phase"] in ("done", "failed"):
            break
        time.sleep(0.05)

    snapshot = storage.get_job(job.id)
    assert snapshot is not None
    assert snapshot["phase"] == "done", snapshot
    assert snapshot["result"]["files"] == 6
    assert done and done[0]["files"] == 6
    assert (target / "papers" / "b.pdf").is_file()


def test_background_job_reports_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "old"
    target = tmp_path / "new"
    _make_library(source)

    def boom(*_args, **_kwargs):
        raise OSError("磁盘已满")

    monkeypatch.setattr(storage, "_copy_file", boom)
    job = storage.start_relocate(source, target)
    deadline = time.monotonic() + 10
    snapshot = None
    while time.monotonic() < deadline:
        snapshot = storage.get_job(job.id)
        assert snapshot is not None
        if snapshot["phase"] in ("done", "failed"):
            break
        time.sleep(0.05)

    assert snapshot is not None
    assert snapshot["phase"] == "failed"
    assert "磁盘已满" in snapshot["error"]
    assert not target.exists()


def test_relocate_rejects_non_empty_target(tmp_path: Path) -> None:
    source = tmp_path / "old"
    target = tmp_path / "new"
    _make_library(source)
    target.mkdir()
    (target / "keep.txt").write_text("x", encoding="utf-8")

    with pytest.raises(ValueError, match="空的"):
        storage.relocate(source, target)


def test_relocate_rejects_nested_target(tmp_path: Path) -> None:
    source = tmp_path / "old"
    _make_library(source)

    with pytest.raises(ValueError, match="不能"):
        storage.start_relocate(source, source / "inside")


def test_only_one_active_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "old"
    _make_library(source)
    real_copy = storage._copy_file

    def slow_copy(*args, **kwargs):
        time.sleep(0.4)
        return real_copy(*args, **kwargs)

    monkeypatch.setattr(storage, "_copy_file", slow_copy)

    first = storage.start_relocate(source, tmp_path / "one")
    try:
        with pytest.raises(ValueError, match="正在进行"):
            storage.start_relocate(source, tmp_path / "two")
    finally:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            snapshot = storage.get_job(first.id)
            if snapshot and snapshot["phase"] in ("done", "failed"):
                break
            time.sleep(0.05)