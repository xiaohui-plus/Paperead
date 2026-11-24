from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import pytest

from server import babeldoc_engine, babeldoc_setup


def _fake_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("python/python.exe", b"stub")
        bundle.writestr("run_babeldoc.py", "print('stub')\n")
    return buffer.getvalue()


def _release(tmp_path: Path, data: bytes, pieces: int = 2) -> tuple[Path, dict]:
    release = tmp_path / "release"
    release.mkdir()
    names: list[str] = []
    step = max(1, len(data) // pieces)
    index = 0
    offset = 0
    while offset < len(data):
        index += 1
        name = f"babeldoc-engine-0.7.1-windows-x86_64.zip.part{index:03d}"
        (release / name).write_bytes(data[offset : offset + step])
        names.append(name)
        offset += step
    manifest = {
        "version": "0.7.1",
        "platform": "windows-x86_64",
        "asset": "babeldoc-engine-0.7.1-windows-x86_64.zip",
        "parts": names,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    return release, manifest


def test_launcher_accepts_portable_python(tmp_path: Path) -> None:
    python = tmp_path / "python" / "python.exe"
    python.parent.mkdir()
    python.write_bytes(b"stub")
    (tmp_path / "run_babeldoc.py").write_text("print('x')\n", encoding="utf-8")

    command = babeldoc_engine.launcher(tmp_path)

    assert command is not None
    assert command[0] == str(python)
    assert command[1] == "-u"
    assert command[2].endswith("run_babeldoc.py")


def test_launcher_accepts_venv_exe(tmp_path: Path) -> None:
    exe = tmp_path / "Scripts" / "babeldoc.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"stub")

    assert babeldoc_engine.launcher(tmp_path) == [str(exe)]


def test_install_downloads_parts_and_extracts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _fake_zip()
    release, manifest = _release(tmp_path, data)
    data_dir = tmp_path / "data"
    monkeypatch.setenv("PAPEREAD_DATA", str(data_dir))
    babeldoc_setup._STATE.update(status="idle", percent=0, error="", startedAt=0, finishedAt=0)

    target = babeldoc_setup._install(manifest, release.as_uri())

    assert babeldoc_engine.launcher(target) is not None
    assert (target / "python" / "python.exe").is_file()
    assert babeldoc_setup.status()["home"] == str(target)
    # 临时下载目录要清干净
    assert not list((data_dir / "engines" / "babeldoc").glob(".download-*"))


def test_install_rejects_bad_checksum(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _fake_zip()
    release, manifest = _release(tmp_path, data)
    manifest["sha256"] = "0" * 64
    monkeypatch.setenv("PAPEREAD_DATA", str(tmp_path / "data"))

    with pytest.raises(ValueError):
        babeldoc_setup._install(manifest, release.as_uri())

    assert not babeldoc_setup.install_dir().exists()


def test_install_falls_back_to_secondary_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """主源只上传了清单、分卷不全时，同一份清单可跨源拼出完整压缩包。

    真实场景：Gitee 固定 latest 发行版漏传 part001/002，旧逻辑整轮必然失败。
    """
    data = _fake_zip()
    release, manifest = _release(tmp_path, data)
    backup = tmp_path / "backup"
    backup.mkdir()
    first = manifest["parts"][0]
    (release / first).rename(backup / first)
    monkeypatch.setenv("PAPEREAD_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(
        babeldoc_setup,
        "_download_bases",
        lambda primary: [primary, backup.as_uri()],
    )

    target = babeldoc_setup._install(manifest, release.as_uri())

    assert (target / "run_babeldoc.py").is_file()
    # 装到当前期望版本目录后 ready 才算真就绪
    assert babeldoc_setup.status()["ready"] is True


def test_safe_names_reject_traversal() -> None:
    with pytest.raises(ValueError):
        babeldoc_setup._safe_names("parts", ["../evil.zip"])
    with pytest.raises(ValueError):
        babeldoc_setup._safe_names("parts", [])


def test_fetch_json_reports_gitee_empty_repo_message(tmp_path: Path) -> None:
    """Gitee 空仓库返回 200 + message；要报真实原因，而不是「清单版本号非法」。"""
    payload = tmp_path / "empty.json"
    payload.write_text(
        '{"message":"空仓库不允许进行的操作","status":200}', encoding="utf-8"
    )

    with pytest.raises(ValueError, match="空仓库"):
        babeldoc_setup._fetch_json(payload.as_uri())


def test_auto_download_is_skipped_in_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAPEREAD_MOCK", "1")
    babeldoc_setup._STATE.update(status="idle")

    babeldoc_setup.start_if_missing()

    assert babeldoc_setup._STATE["status"] == "idle"
