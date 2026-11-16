"""文献库目录迁移：把当前数据目录完整复制到用户选择的新位置。

迁移只复制、不删除源目录；旧目录等应用重启、文件句柄释放后再由
``runtime.cleanup_previous_data_dir`` 清理。SQLite 数据库走 backup API，
避免直接复制 WAL 导致的半截数据。

复制在后台线程里执行，进度由 :class:`RelocationProgress` 汇总，前端轮询
``GET /api/storage/relocate/{job_id}`` 显示百分比；普通文件并发复制，
SQLite 文件单独串行处理。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

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

# 单次读写块大小与并发度：大块减少系统调用，多线程让大量小文件（页图、
# 解析产物）不再逐个排队；机械盘上并发过高会加重寻道，上限控制在 8。
COPY_CHUNK = 4 * 1024 * 1024
COPY_WORKERS = max(1, min(8, (os.cpu_count() or 4)))

_ACTIVE_PHASES = {"starting", "scanning", "copying", "finalizing"}
_JOB_RETENTION = 10 * 60


def _is_sqlite(path: Path) -> bool:
    return path.suffix.lower() in SQLITE_SUFFIXES


def _skip(name: str) -> bool:
    return name in SKIP_NAMES or name.endswith(SKIP_SUFFIXES)


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


def _copy_file(
    source: Path,
    target: Path,
    progress: "RelocationProgress | None" = None,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(source, "rb") as src, open(target, "wb") as dst:
        while True:
            chunk = src.read(COPY_CHUNK)
            if not chunk:
                break
            dst.write(chunk)
            if progress is not None:
                progress.add_bytes(len(chunk))
    try:
        shutil.copystat(source, target)
    except OSError:
        pass


@dataclass
class _Plan:
    dirs: list[Path] = field(default_factory=list)
    files: list[tuple[Path, Path, int]] = field(default_factory=list)
    sqlite_files: list[tuple[Path, Path, int]] = field(default_factory=list)
    total_bytes: int = 0

    @property
    def total_files(self) -> int:
        return len(self.files) + len(self.sqlite_files)


def _scan(source: Path) -> _Plan:
    plan = _Plan()
    for current, dirnames, filenames in os.walk(source):
        base = Path(current)
        rel_dir = base.relative_to(source)
        dirnames[:] = [
            name
            for name in dirnames
            if name not in SKIP_DIRS and not _skip(name)
        ]
        plan.dirs.extend(rel_dir / name for name in dirnames)
        for name in filenames:
            if _skip(name):
                continue
            path = base / name
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            rel = rel_dir / name
            if _is_sqlite(path):
                plan.sqlite_files.append((path, rel, size))
            else:
                plan.files.append((path, rel, size))
            plan.total_bytes += size
    return plan


class RelocationProgress:
    """后台迁移的实时进度；读写都在锁内，供 HTTP 轮询安全读取。"""

    def __init__(self, target: Path) -> None:
        self.id = uuid.uuid4().hex
        self.target = str(target)
        self._lock = threading.Lock()
        self.phase = "starting"
        self.files_done = 0
        self.files_total = 0
        self.bytes_done = 0
        self.bytes_total = 0
        self.current = ""
        self.error = ""
        self.result: dict[str, Any] | None = None
        self.started_at = time.monotonic()
        self.finished_at = 0.0

    def set_phase(self, phase: str) -> None:
        with self._lock:
            self.phase = phase

    def begin(self, files_total: int, bytes_total: int) -> None:
        with self._lock:
            self.phase = "copying"
            self.files_total = files_total
            self.bytes_total = bytes_total

    def add_bytes(self, size: int) -> None:
        with self._lock:
            self.bytes_done += max(0, size)

    def file_started(self, name: str) -> None:
        with self._lock:
            self.current = name

    def file_done(self) -> None:
        with self._lock:
            self.files_done += 1

    def finish(self, result: dict[str, Any]) -> None:
        with self._lock:
            self.phase = "done"
            self.result = result
            self.files_done = max(self.files_done, self.files_total)
            self.bytes_done = max(self.bytes_done, self.bytes_total)
            self.current = ""
            self.finished_at = time.monotonic()

    def fail(self, message: str) -> None:
        with self._lock:
            self.phase = "failed"
            self.error = message or "迁移失败"
            self.current = ""
            self.finished_at = time.monotonic()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            end = self.finished_at or time.monotonic()
            elapsed = max(0.0, end - self.started_at)
            speed = self.bytes_done / elapsed if elapsed > 0 else 0.0
            data: dict[str, Any] = {
                "job_id": self.id,
                "phase": self.phase,
                "target": self.target,
                "files_done": self.files_done,
                "files_total": self.files_total,
                "bytes_done": self.bytes_done,
                "bytes_total": self.bytes_total,
                "current": self.current,
                "error": self.error,
                "elapsed": round(elapsed, 1),
                "speed": round(speed, 1),
            }
            if self.result is not None:
                data["result"] = self.result
            return data


_jobs: dict[str, RelocationProgress] = {}
_jobs_lock = threading.Lock()


def _prune_jobs() -> None:
    now = time.monotonic()
    stale = [
        job_id
        for job_id, job in _jobs.items()
        if job.finished_at and now - job.finished_at > _JOB_RETENTION
    ]
    for job_id in stale:
        _jobs.pop(job_id, None)


def get_job(job_id: str) -> dict[str, Any] | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
    return job.snapshot() if job is not None else None


def start_relocate(
    source: Path,
    target: Path,
    on_success: Callable[[dict[str, Any]], None] | None = None,
) -> RelocationProgress:
    """校验目标后启动后台迁移，立即返回可轮询的进度对象。"""
    source = source.expanduser().resolve()
    target = target.expanduser().resolve()
    if not source.is_dir():
        raise ValueError("当前文献库目录不存在")
    _validate(source, target)

    with _jobs_lock:
        if any(job.phase in _ACTIVE_PHASES for job in _jobs.values()):
            raise ValueError("已有迁移任务正在进行中，请等待完成")
        progress = RelocationProgress(target)
        _jobs[progress.id] = progress
        _prune_jobs()

    def run() -> None:
        try:
            result = relocate(source, target, progress)
            progress.set_phase("finalizing")
            if on_success is not None:
                on_success(result)
            progress.finish(result)
        except Exception as error:  # noqa: BLE001 - 失败原因要原样回给界面
            progress.fail(str(error) or error.__class__.__name__)

    threading.Thread(target=run, name="storage-relocate", daemon=True).start()
    return progress


def _validate(source: Path, target: Path) -> None:
    if target == source:
        raise ValueError("新路径与当前文献库路径相同")
    if source in target.parents or target in source.parents:
        raise ValueError("新路径不能位于当前文献库目录内，也不能包含当前目录")
    if target.exists() and not target.is_dir():
        raise ValueError("新路径已存在且不是文件夹")
    if target.exists() and any(target.iterdir()):
        raise ValueError("目标文件夹不是空的，请选择一个空文件夹")


def _probe_writable(target: Path) -> None:
    probe = target / ".paperead-write-test"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as error:
        raise ValueError(f"目标文件夹不可写：{error}") from error


def _cleanup_target(target: Path, created: bool) -> None:
    if created:
        shutil.rmtree(target, ignore_errors=True)
        return
    for child in target.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            try:
                child.unlink()
            except OSError:
                pass


def _copy_plan(
    plan: _Plan,
    target: Path,
    progress: RelocationProgress | None,
    stats: dict[str, Any],
) -> None:
    for rel in plan.dirs:
        (target / rel).mkdir(parents=True, exist_ok=True)

    # SQLite 先串行做完：备份 API 会持有源库读锁，与并发写盘混在一起容易
    # 拉长单库耗时，也会让进度条前半段几乎不动。
    for source, rel, size in plan.sqlite_files:
        if progress is not None:
            progress.file_started(rel.as_posix())
        child = target / rel
        try:
            _copy_sqlite(source, child)
        except sqlite3.Error:
            # 不是真正的 SQLite 文件（例如误命名的附件）：按普通文件复制

            shutil.copy2(source, child)
        if progress is not None:
            progress.add_bytes(size)
            progress.file_done()
        stats["files"] += 1
        stats["bytes"] += size

    def copy_one(item: tuple[Path, Path, int]) -> int:
        source, rel, size = item
        if progress is not None:
            progress.file_started(rel.as_posix())
        _copy_file(source, target / rel, progress)
        if progress is not None:
            progress.file_done()
        return size

    if not plan.files:
        return
    workers = min(COPY_WORKERS, len(plan.files))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="storage-copy") as pool:
        futures = [pool.submit(copy_one, item) for item in plan.files]
        try:
            for future in as_completed(futures):
                size = future.result()
                stats["files"] += 1
                stats["bytes"] += size
        except Exception:
            for future in futures:
                future.cancel()
            raise


def relocate(
    source: Path,
    target: Path,
    progress: RelocationProgress | None = None,
) -> dict:
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
    _probe_writable(target)

    if progress is not None:
        progress.set_phase("scanning")
    plan = _scan(source)
    if progress is not None:
        progress.begin(plan.total_files, plan.total_bytes)

    stats: dict[str, Any] = {"files": 0, "bytes": 0}
    try:
        _copy_plan(plan, target, progress, stats)
        if (source / "library.json").is_file() and not (target / "library.json").is_file():
            raise RuntimeError("文献索引没有复制成功")
        if (source / "research" / "research.db").is_file() and not (
            target / "research" / "research.db"
        ).is_file():
            raise RuntimeError("研究数据库没有复制成功")
    except Exception:
        _cleanup_target(target, created)
        raise

    return {
        "ok": True,
        "path": str(target),
        "previous": str(source),
        "files": stats["files"],
        "bytes": stats["bytes"],
    }