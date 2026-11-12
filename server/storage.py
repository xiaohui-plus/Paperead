"""文献库目录迁移：把当前数据目录完整复制到用户选择的新位置。

迁移只复制、不删除源目录；旧目录等应用重启、文件句柄释放后再由
``runtime.cleanup_previous_data_dir`` 清理。SQLite 数据库走 backup API，
避免直接复制 WAL 导致的半截数据。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any

# 可重建或与迁移目标无关的目录
SKIP_DIRS = {"logs", "tmp", "thumb-cache", "tools"}
SKIP_SUFFIXES = (
    ".lock",
    ".tmp",
    "-wal",
    "-shm",
    ".download",
    ".partial",
)
SKIP_NAMES = {".DS_Store", "desktop.ini", "Thumbs.db"}
SQLITE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}


def _is_sqlite(path: Path) -> bool:
    return path.suffix.lower() in SQLITE_SUFFIXES


def _copy_sqlite(source: Path, target: Path) -> None:
    """用 SQLite 在线备份复制，保证数据库文件自洽。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True, timeout=10)
    try:
        dst = sqlite3.connect(str(target), timeout=10)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _copy_tree(source: Path, target: Path, stats: dict[str, Any]) -> None:
    for entry in source.iterdir():
        name = entry.name
        if name in SKIP_NAMES or name.endswith(SKIP_SUFFIXES):
            continue
        if entry.is_dir():
            if name in SKIP_DIRS:
                continue
            child = target / name
            child.mkdir(parents=True, exist_ok=True)
            _copy_tree(entry, child, stats)
            continue
        if not entry.is_file():
            continue
        child = target / name
        child.parent.mkdir(parents=True, exist_ok=True)
        if _is_sqlite(entry):
            try:
                _copy_sqlite(entry, child)
            except sqlite3.Error:
                # 不是真正的 SQLite 文件（例如误命名的附件）：按普通文件复制
                shutil.copy2(entry, child)
        else:
            shutil.copy2(entry, child)
        stats["files"] += 1
        try:
            stats["bytes"] += child.stat().st_size
        except OSError:
            pass


def _validate(source: Path, target: Path) -> None:
    if target == source:
        raise ValueError("新路径与当前文献库路径相同")
    if source in target.parents or target in source.parents:
        raise ValueError("新路径不能位于当前文献库目录内，也不能包含当前目录")
    if target.exists() and not target.is_dir():
        raise ValueError("新路径已存在且不是文件夹")
    if target.exists() and any(target.iterdir()):
        raise ValueError("目标文件夹不是空的，请选择一个空文件夹")


def relocate(source: Path, target: Path) -> dict:
    """把 ``source`` 复制到 ``target``，返回迁移统计。

    失败时清掉目标目录里已写入的内容，源目录始终保持原样。
    """
    source = source.expanduser().resolve()
    target = target.expanduser().resolve()
    if not source.is_dir():
        raise ValueError("当前文献库目录不存在")
    _validate(source, target)

    created = not target.exists()
    target.mkdir(parents=True, exist_ok=True)
    probe = target / ".paperead-write-test"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as error:
        raise ValueError(f"目标文件夹不可写：{error}") from error

    stats: dict[str, Any] = {"files": 0, "bytes": 0}
    try:
        _copy_tree(source, target, stats)
        if (source / "library.json").is_file() and not (target / "library.json").is_file():
            raise RuntimeError("文献索引没有复制成功")
        if (source / "research" / "research.db").is_file() and not (
            target / "research" / "research.db"
        ).is_file():
            raise RuntimeError("研究数据库没有复制成功")
    except Exception:
        if created:
            shutil.rmtree(target, ignore_errors=True)
        else:
            for child in target.iterdir():
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    try:
                        child.unlink()
                    except OSError:
                        pass
        raise

    return {
        "ok": True,
        "path": str(target),
        "previous": str(source),
        "files": stats["files"],
        "bytes": stats["bytes"],
    }