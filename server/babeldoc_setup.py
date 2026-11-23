"""BabelDOC 引擎的后台下载与安装。

安装包不内置引擎（解压后约 600MB，超出 Gitee 单附件限制）：首次启动在
后台从发行版下载分卷，校验 SHA-256 后解压到
``<数据目录>/engines/babeldoc/<version>``。

整个流程只用 ``urllib`` 读网络、``zipfile`` 解压，不创建任何子进程，
因此不会弹出 cmd / PowerShell 窗口。下载失败时静默记录状态，翻译仍会
回退到分块模型路径，不影响应用启动。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path
from typing import Any

from server import babeldoc_engine

log = logging.getLogger("paperead.babeldoc")

ENGINE_VERSION = "0.7.1"
PLATFORM = "windows-x86_64"
MANIFEST_NAME = f"babeldoc-engine-{ENGINE_VERSION}.json"
ZIP_NAME = f"babeldoc-engine-{ENGINE_VERSION}-{PLATFORM}.zip"
CHUNK = 256 * 1024
_VERSION_RE = re.compile(r"^[0-9][0-9A-Za-z._-]*$")

_LOCK = threading.Lock()
_THREAD: threading.Thread | None = None
_STATE: dict[str, Any] = {
    "status": "idle",  # idle | running | done | error
    "percent": 0,
    "error": "",
    "startedAt": 0,
    "finishedAt": 0,
}


def _data_root() -> Path:
    from server.runtime import default_data_dir

    return default_data_dir()


def install_dir() -> Path:
    """引擎最终落点：``<数据目录>/engines/babeldoc/<version>``。"""
    return babeldoc_engine.engine_root() / ENGINE_VERSION


def installed() -> bool:
    """是否已经有可用的引擎（开发态指向 .refs 也算）。"""
    return babeldoc_engine.engine_home() is not None


def _ready() -> bool:
    """当前 App 期望的引擎版本是否已经就位；旧版本不算，方便随版本升级。"""
    explicit = os.environ.get("PAPEREAD_BABELDOC_HOME", "").strip()
    if explicit:
        return babeldoc_engine.launcher(Path(explicit).expanduser()) is not None
    return babeldoc_engine.launcher(install_dir()) is not None


def _manifest_sources() -> list[str]:
    override = os.environ.get("PAPEREAD_BABELDOC_ENGINE_URL", "").strip()
    if override:
        return [override]
    from paperead import __version__

    return [
        f"https://gitee.com/deng_xiaohui/paperead/releases/download/latest/{MANIFEST_NAME}",
        f"https://gitee.com/deng_xiaohui/paperead/releases/download/v{__version__}/{MANIFEST_NAME}",
        f"https://ghproxy.net/https://github.com/xiaohui-plus/Paperead/releases/latest/download/{MANIFEST_NAME}",
        f"https://github.com/xiaohui-plus/Paperead/releases/latest/download/{MANIFEST_NAME}",
    ]


def status() -> dict[str, Any]:
    with _LOCK:
        data = dict(_STATE)
    data["installed"] = installed()
    data["version"] = ENGINE_VERSION
    data["home"] = str(babeldoc_engine.engine_home() or install_dir())
    return data


def _set(**fields: Any) -> None:
    with _LOCK:
        _STATE.update(fields)


def _fetch_json(url: str, timeout: int = 30) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "paperead"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("清单格式不正确")
    return payload


def _download(url: str, dest: Path, on_bytes) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "paperead"})
    with urllib.request.urlopen(request, timeout=60) as response:
        with dest.open("wb") as handle:
            while True:
                block = response.read(CHUNK)
                if not block:
                    break
                handle.write(block)
                on_bytes(len(block))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _safe_names(prefix: str, values: Any) -> list[str]:
    """校验分卷文件名，挡住 ``..``、绝对路径和远程路径。"""
    if not isinstance(values, list) or not values:
        raise ValueError(f"{prefix} 为空")
    names: list[str] = []
    for value in values:
        name = str(value or "").strip()
        if not name or name != os.path.basename(name) or name in {".", ".."}:
            raise ValueError(f"{prefix} 含非法文件名：{name}")
        names.append(name)
    return names


def _safe_extract(archive: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    root = target.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            dest = (target / info.filename).resolve()
            if dest != root and root not in dest.parents:
                raise ValueError(f"压缩包路径非法：{info.filename}")
        bundle.extractall(target)


def _cleanup_stale(root: Path, max_age: float = 86400) -> None:
    """只清理一天前的残留下载目录，避免误删另一个正在下载的进程。"""
    now = time.time()
    for child in root.glob(".download-*"):
        try:
            if now - child.stat().st_mtime < max_age:
                continue
        except OSError:
            continue
        shutil.rmtree(child, ignore_errors=True)


def _install(manifest: dict[str, Any], base_url: str) -> Path:
    version = str(manifest.get("version") or "").strip()
    if not _VERSION_RE.match(version):
        raise ValueError(f"清单版本号非法：{version}")
    if str(manifest.get("platform") or PLATFORM) != PLATFORM:
        raise ValueError("清单不是当前平台的引擎")
    parts = _safe_names("parts", manifest.get("parts"))
    expected_sha = str(manifest.get("sha256") or "").strip().lower()
    if not expected_sha:
        raise ValueError("清单缺少 SHA-256")
    try:
        expected_size = int(manifest.get("size") or 0)
    except (TypeError, ValueError):
        expected_size = 0

    root = babeldoc_engine.engine_root()
    root.mkdir(parents=True, exist_ok=True)
    _cleanup_stale(root)

    work = root / f".download-{uuid.uuid4().hex[:10]}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        downloaded = 0

        def on_bytes(count: int) -> None:
            nonlocal downloaded
            downloaded += count
            if expected_size > 0:
                # 2% 留给清单，下载阶段最多到 88%
                _set(percent=min(88, 2 + int(downloaded * 86 / expected_size)))

        archive = work / ZIP_NAME
        with archive.open("wb") as handle:
            for name in parts:
                url = f"{base_url}/{urllib.parse.quote(name)}"
                temp = work / f"{name}.part"
                _download(url, temp, on_bytes)
                with temp.open("rb") as source:
                    shutil.copyfileobj(source, handle, CHUNK)
                temp.unlink(missing_ok=True)

        if expected_size and archive.stat().st_size != expected_size:
            raise ValueError("下载体积与清单不一致")
        if _sha256(archive) != expected_sha:
            raise ValueError("下载内容校验失败，请重试")

        _set(percent=90)
        stage = work / "tree"
        _safe_extract(archive, stage)
        if babeldoc_engine.launcher(stage) is None:
            raise ValueError("压缩包里没有可用的 BabelDOC 启动文件")
        _set(percent=97)

        target = root / version
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            # 另一个进程已经装好了：直接用现成的。
            return target
        try:
            os.replace(stage, target)
        except OSError:
            if not target.exists():
                raise
        log.info("BabelDOC 引擎已安装：%s", target)
        return target
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _run() -> None:
    errors: list[str] = []
    for manifest_url in _manifest_sources():
        try:
            _set(percent=1, error="")
            manifest = _fetch_json(manifest_url)
            base_url = manifest_url.rsplit("/", 1)[0]
            _install(manifest, base_url)
            _set(status="done", percent=100, finishedAt=int(time.time()))
            return
        except (
            OSError,
            ValueError,
            zipfile.BadZipFile,
            urllib.error.URLError,
            json.JSONDecodeError,
        ) as error:
            host = urllib.parse.urlparse(manifest_url).netloc or manifest_url
            errors.append(f"{host}: {error}")
            log.warning("BabelDOC 引擎下载失败（%s）：%s", host, error)
    _set(
        status="error",
        error="；".join(errors)[-800:] or "下载失败",
        finishedAt=int(time.time()),
    )


def start() -> dict[str, Any]:
    """开始后台下载；当前版本已就位或正在下载时直接返回当前状态。"""
    global _THREAD
    with _LOCK:
        if _ready():
            _STATE.update(status="done", percent=100)
            return dict(_STATE)
        if _THREAD is not None and _THREAD.is_alive():
            return dict(_STATE)
        _STATE.update(status="running", percent=0, error="", startedAt=int(time.time()))
        _THREAD = threading.Thread(target=_run, daemon=True, name="babeldoc-install")
        _THREAD.start()
    return status()


def start_if_missing() -> None:
    """应用启动时调用：没装过就悄悄下载，不阻塞启动。"""
    if os.environ.get("PAPEREAD_MOCK") == "1":
        return
    if os.environ.get("PAPEREAD_BABELDOC", "1") == "0":
        return
    if os.name != "nt":
        # 目前只发布 Windows 引擎；其他平台保持分块翻译回退。
        return
    if _ready():
        return
    st = status()
    if st["status"] == "running":
        return
    try:
        start()
    except OSError:
        pass
