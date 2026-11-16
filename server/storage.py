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
from collections import deque
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
COPY_CHUNK = 8 * 1024 * 1024
COPY_WORKERS = max(1, min(8, (os.cpu_count() or 4)))
# 大文件（PDF/模型/数据库）走系统复制引擎并单独控制并发：同盘或外接盘上
# 多个大文件并行会互相抢磁头，2 路足够，再大反而降速。
LARGE_FILE = 8 * 1024 * 1024
LARGE_WORKERS = 2

_ACTIVE_PHASES = {"starting", "scanning", "copying", "finalizing"}
# 任务完成/失败后在内存里多留一段时间：用户切去阅读页再切回来还能看到结果。
_JOB_RETENTION = 60 * 60


def _is_sqlite(path: Path) -> bool:
    return path.suffix.lower() in SQLITE_SUFFIXES


def _skip(name: str) -> bool:
    return name in SKIP_NAMES or name.endswith(SKIP_SUFFIXES)


def _copy_sqlite(
    source: Path,
    target: Path,
    progress: "RelocationProgress | None" = None,
    size: int = 0,
) -> None:
    """用 SQLite 在线备份复制，保证数据库文件自洽。

    分批备份并回报进度：大库（几百 MB）一次备份时界面会长时间不动，
    按页数折算成字节后进度条可以平滑推进。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True, timeout=10)
    added = 0

    def on_backup(_status: int, remaining: int, total: int) -> None:
        nonlocal added
        if progress is None or total <= 0 or size <= 0:
            return
        done = int(size * max(0.0, 1.0 - remaining / total))
        if done > added:
            progress.add_bytes(done - added)
            added = done

    try:
        dst = sqlite3.connect(str(target), timeout=10)
        try:
            # 每 2048 页一批：比默认 100 页快，又比一次性备份能回报进度；
            # sleep=0 避免每批之间人为等待。
            src.backup(
                dst,
                pages=2048,
                sleep=0,
                progress=on_backup if progress is not None else None,
            )
        finally:
            dst.close()
        if progress is not None and size > added:
            progress.add_bytes(size - added)
    except sqlite3.Error:
        # 回退成普通文件复制前，把已经计入的进度退回，避免重复累加
        if progress is not None and added:
            progress.add_bytes(-added)
        raise
    finally:
        src.close()


_WINDOWS_PROGRESS_TYPE = None


def _windows_progress_type():
    """LPPROGRESS_ROUTINE 的 ctypes 签名；只在 Windows 上初始化一次。"""
    global _WINDOWS_PROGRESS_TYPE
    if _WINDOWS_PROGRESS_TYPE is None:
        import ctypes
        from ctypes import wintypes

        _WINDOWS_PROGRESS_TYPE = ctypes.WINFUNCTYPE(
            wintypes.DWORD,
            ctypes.c_longlong,
            ctypes.c_longlong,
            ctypes.c_longlong,
            ctypes.c_longlong,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.c_void_p,
        )
    return _WINDOWS_PROGRESS_TYPE


def _copy_file_native(
    source: Path,
    target: Path,
    progress: "RelocationProgress | None" = None,
) -> bool:
    """Windows 上用 CopyFileExW 走系统复制引擎。

    比 Python 的 read/write 循环少了逐块跨语言调度，外接盘和跨盘复制通常
    更快；失败（如目标文件系统不支持）时返回 False，由调用方回退到普通复制。
    复制过程中按系统回调实时累加字节，大文件不会等整份复制完才推进进度。
    """
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    copy_file_ex = kernel32.CopyFileExW
    callback_type = _windows_progress_type()
    copy_file_ex.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        callback_type,
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        wintypes.DWORD,
    ]
    copy_file_ex.restype = wintypes.BOOL

    state = {"done": 0}

    def on_progress(
        _total,
        transferred,
        _stream_size,
        _stream_transferred,
        _stream_no,
        _reason,
        _src,
        _dst,
        _data,
    ):
        current = int(transferred)
        delta = current - state["done"]
        if delta > 0:
            state["done"] = current
            if progress is not None:
                progress.add_bytes(delta)
        return 0  # PROGRESS_CONTINUE

    # 不传进度时完全不加回调：系统复制最少的跨语言开销。
    # 大文件需要逐块进度，由调用方改用 Python 分块复制。
    callback = callback_type(on_progress) if progress is not None else callback_type()
    try:
        ok = copy_file_ex(str(source), str(target), callback, None, None, 0)
        if ok:
            return True
        error = ctypes.get_last_error()
    except OSError:
        error = ctypes.get_last_error()

    # 失败时把已计入的进度退回，避免回退重拷后百分比超过 100
    if progress is not None and state["done"]:
        progress.add_bytes(-state["done"])
    # 路径不存在/权限/不支持等可回退；磁盘满等真错误交给上层报失败
    if error in (0, 2, 3, 5, 53, 123):
        return False
    raise OSError(error, "系统复制失败")


def _copy_times(source: Path, target: Path) -> None:
    """普通文件复制后只保留时间戳；权限位在 Windows 上无意义，copystat 更慢。"""
    try:
        stat = source.stat()
        os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    except OSError:
        pass


def _copy_file(
    source: Path,
    target: Path,
    progress: "RelocationProgress | None" = None,
    *,
    native: bool = True,
    size: int | None = None,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    # 小文件用系统复制（无回调，吞吐最高），完成时一次性记进度；
    # 大文件改用下面的分块循环，保证复制途中进度条持续前进。
    if native and _copy_file_native(source, target, None):
        if progress is not None:
            progress.add_bytes(size if size is not None else source.stat().st_size)
        return
    with open(source, "rb") as src, open(target, "wb") as dst:
        while True:
            chunk = src.read(COPY_CHUNK)
            if not chunk:
                break
            dst.write(chunk)
            if progress is not None:
                progress.add_bytes(len(chunk))
    _copy_times(source, target)


@dataclass
class _Plan:
    dirs: list[Path] = field(default_factory=list)
    files: list[tuple[Path, Path, int]] = field(default_factory=list)
    sqlite_files: list[tuple[Path, Path, int]] = field(default_factory=list)
    total_bytes: int = 0

    @property
    def total_files(self) -> int:
        return len(self.files) + len(self.sqlite_files)


def _scan(
    source: Path,
    progress: "RelocationProgress | None" = None,
) -> _Plan:
    plan = _Plan()
    scanned_files = 0
    scanned_bytes = 0
    if progress is not None:
        progress.begin_scan()
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
            scanned_files += 1
            scanned_bytes += size
            # 文件很多或目录在网络盘上时，扫描本身也要让界面看到进展
            if progress is not None and scanned_files % 64 == 0:
                progress.scan_progress(scanned_files, scanned_bytes)
    if progress is not None:
        progress.scan_progress(scanned_files, scanned_bytes)
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
        self.files_scanned = 0
        self.bytes_scanned = 0
        self.current = ""
        self.error = ""
        self.result: dict[str, Any] | None = None
        self.started_at = time.monotonic()
        self.scan_started_at = self.started_at
        self.copy_started_at = 0.0
        self.finished_at = 0.0
        # 复制速度单独用高精度计时：monotonic 在 Windows 的粒度约 15ms，
        # 小库复制会在同一刻度内完成，速度被算成 0。
        self._copy_perf = 0.0
        self._finish_perf = 0.0
        # 最近两秒的采样用于实时速度；平均速度仍按复制阶段单独计时，
        # 不把扫描/等待时间算进去（否则大库看起来只有几 MB/s）。
        self._samples: deque[tuple[float, int]] = deque(maxlen=240)

    def set_phase(self, phase: str) -> None:
        with self._lock:
            self.phase = phase

    def begin_scan(self) -> None:
        with self._lock:
            self.phase = "scanning"
            self.scan_started_at = time.monotonic()

    def scan_progress(self, files: int, total_bytes: int) -> None:
        with self._lock:
            self.files_scanned = max(self.files_scanned, files)
            self.bytes_scanned = max(self.bytes_scanned, total_bytes)

    def begin(self, files_total: int, bytes_total: int) -> None:
        with self._lock:
            self.phase = "copying"
            self.files_total = files_total
            self.bytes_total = bytes_total
            self.copy_started_at = time.monotonic()
            self._copy_perf = time.perf_counter()
            self._samples.clear()
            self._samples.append((self.copy_started_at, 0))

    def add_bytes(self, size: int) -> None:
        with self._lock:
            # 系统复制失败回退时会传负值，把之前计入的部分退掉
            self.bytes_done = max(0, self.bytes_done + size)
            self._samples.append((time.monotonic(), self.bytes_done))

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
            self._finish_perf = time.perf_counter()

    def fail(self, message: str) -> None:
        with self._lock:
            self.phase = "failed"
            self.error = message or "迁移失败"
            self.current = ""
            self.finished_at = time.monotonic()
            self._finish_perf = time.perf_counter()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = self.finished_at or time.monotonic()
            elapsed = max(0.0, now - self.started_at)
            # 速度用 perf_counter 计时，避免小库复制落在同一毫秒刻度里算出 0
            copy_now = self._finish_perf or time.perf_counter()
            copy_elapsed = (
                max(0.0, copy_now - self._copy_perf) if self._copy_perf else 0.0
            )
            speed = self.bytes_done / copy_elapsed if copy_elapsed > 0 else 0.0

            instant = 0.0
            if self.copy_started_at and len(self._samples) >= 2:
                cutoff = now - 2.0
                recent = [pair for pair in self._samples if pair[0] >= cutoff]
                if len(recent) >= 2:
                    span = recent[-1][0] - recent[0][0]
                    if span > 0:
                        instant = (recent[-1][1] - recent[0][1]) / span
            if instant <= 0:
                instant = speed

            remaining = max(0, self.bytes_total - self.bytes_done)
            eta = remaining / instant if instant > 0 else 0.0
            data: dict[str, Any] = {
                "job_id": self.id,
                "phase": self.phase,
                "target": self.target,
                "files_done": self.files_done,
                "files_total": self.files_total,
                "bytes_done": self.bytes_done,
                "bytes_total": self.bytes_total,
                "files_scanned": self.files_scanned,
                "bytes_scanned": self.bytes_scanned,
                "current": self.current,
                "error": self.error,
                "elapsed": round(elapsed, 1),
                "copy_elapsed": round(copy_elapsed, 1),
                "speed": round(speed, 1),
                "instant_speed": round(max(0.0, instant), 1),
                "eta": round(max(0.0, eta), 1),
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


def active_job() -> dict[str, Any] | None:
    """当前正在跑的迁移任务，供前端切换界面后重新挂载时恢复进度。"""
    with _jobs_lock:
        jobs = list(_jobs.values())
    for job in jobs:
        if job.phase in _ACTIVE_PHASES:
            return job.snapshot()
    return None


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
            _copy_sqlite(source, child, progress, size)
        except sqlite3.Error:
            # 不是真正的 SQLite 文件（例如误命名的附件）：按普通文件复制
            _copy_file(source, child, progress, native=False, size=size)
        if progress is not None:
            progress.file_done()
        stats["files"] += 1
        stats["bytes"] += size

    def copy_one(item: tuple[Path, Path, int]) -> int:
        source, rel, size = item
        if progress is not None:
            progress.file_started(rel.as_posix())
        _copy_file(
            source,
            target / rel,
            progress,
            native=size < LARGE_FILE,
            size=size,
        )
        if progress is not None:
            progress.file_done()
        return size

    def run_batch(items: list[tuple[Path, Path, int]], limit: int) -> None:
        if not items:
            return
        workers = max(1, min(limit, len(items)))
        if workers == 1:
            for item in items:
                stats["files"] += 1
                stats["bytes"] += copy_one(item)
            return
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="storage-copy"
        ) as pool:
            futures = [pool.submit(copy_one, item) for item in items]
            try:
                for future in as_completed(futures):
                    size = future.result()
                    stats["files"] += 1
                    stats["bytes"] += size
            except Exception:
                for future in futures:
                    future.cancel()
                raise

    # 大文件顺序读写更吃带宽，单独少量并发；大量小文件（页图、解析产物）
    # 走多线程重叠元数据开销。分两批，避免两类 IO 互相打断。
    large = [item for item in plan.files if item[2] >= LARGE_FILE]
    small = [item for item in plan.files if item[2] < LARGE_FILE]
    run_batch(large, LARGE_WORKERS)
    run_batch(small, COPY_WORKERS)


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
        progress.begin_scan()
    plan = _scan(source, progress)
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