"""pandoc 按需安装。

安装包里不再塞 116MB 的 pandoc：首次启动在后台下载一次，之后离线可用。
下载地址依次尝试 Gitee 发行版、GitHub 发行版（经 ghproxy 镜像）和 GitHub 直连，
任一成功即解压出 ``pandoc.exe`` 放进数据目录 ``tools/pandoc``。

便携版仍然可以自带 pandoc：``server/research/latex.py`` 会优先用安装目录里
``resources/tools/pandoc/pandoc.exe``，找不到才用这里下载的那份。
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

PANDOC_VERSION = "2.19.2"
ASSET_NAME = f"pandoc-{PANDOC_VERSION}-windows-x86_64.zip"
ASSET_SHA256 = "E7A0C92B4AF6CAD31D9899A8B92A3992E18634320349BBF56B729BBBCF71BB99"
CHUNK = 256 * 1024

_LOCK = threading.Lock()
_THREAD: threading.Thread | None = None
_STATE: dict[str, Any] = {
    "status": "idle",  # idle | running | done | error
    "percent": 0,
    "error": "",
    "startedAt": 0,
    "finishedAt": 0,
}


def install_dir() -> Path:
    """下载的 pandoc 存放位置：``<数据目录>/tools/pandoc``。"""
    from server.runtime import default_data_dir

    return default_data_dir() / "tools" / "pandoc"


def installed_exe() -> Path | None:
    target = install_dir() / "pandoc.exe"
    return target if target.is_file() else None


def _sources() -> list[str]:
    override = os.environ.get("PAPEREAD_PANDOC_URL")
    if override:
        return [override]
    from paperead import __version__

    return [
        f"https://gitee.com/deng_xiaohui/paperead/releases/download/v{__version__}/{ASSET_NAME}",
        f"https://ghproxy.net/https://github.com/xiaohui-plus/Paperead/releases/latest/download/{ASSET_NAME}",
        f"https://github.com/xiaohui-plus/Paperead/releases/latest/download/{ASSET_NAME}",
    ]


def status() -> dict[str, Any]:
    with _LOCK:
        data = dict(_STATE)
    data["installed"] = installed_exe() is not None
    data["version"] = PANDOC_VERSION
    return data


def _set(**fields: Any) -> None:
    with _LOCK:
        _STATE.update(fields)


def _download(url: str, target: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "paperead"})
    with urllib.request.urlopen(request, timeout=60) as response:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        with target.open("wb") as handle:
            while True:
                block = response.read(CHUNK)
                if not block:
                    break
                handle.write(block)
                done += len(block)
                if total:
                    _set(percent=min(99, int(done * 100 / total)))
    digest = hashlib.sha256(target.read_bytes()).hexdigest().upper()
    if digest != ASSET_SHA256:
        target.unlink(missing_ok=True)
        raise ValueError("下载内容校验失败，请重试")


def _extract(archive: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        member = next(
            (name for name in bundle.namelist() if name.lower().endswith("pandoc.exe")),
            None,
        )
        if member is None:
            raise ValueError("压缩包里没有 pandoc.exe")
        with bundle.open(member) as source, target.open("wb") as handle:
            while True:
                block = source.read(CHUNK)
                if not block:
                    break
                handle.write(block)


def _run() -> None:
    work = install_dir()
    work.mkdir(parents=True, exist_ok=True)
    archive = work / ASSET_NAME
    errors: list[str] = []
    for url in _sources():
        try:
            _set(percent=0, error="")
            _download(url, archive)
            _extract(archive, work / "pandoc.exe")
            archive.unlink(missing_ok=True)
            _set(status="done", percent=100, finishedAt=int(time.time()))
            return
        except (OSError, ValueError, zipfile.BadZipFile, urllib.error.URLError) as error:
            errors.append(f"{url.split('/')[2]}: {error}")
            archive.unlink(missing_ok=True)
    _set(
        status="error",
        error="；".join(errors)[-800:] or "下载失败",
        finishedAt=int(time.time()),
    )


def start() -> dict[str, Any]:
    """开始后台下载；已经装好或正在下载时直接返回当前状态。"""
    global _THREAD
    with _LOCK:
        if installed_exe() is not None:
            _STATE.update(status="done", percent=100)
            return dict(_STATE)
        if _THREAD is not None and _THREAD.is_alive():
            return dict(_STATE)
        _STATE.update(status="running", percent=0, error="", startedAt=int(time.time()))
        _THREAD = threading.Thread(target=_run, daemon=True, name="pandoc-install")
        _THREAD.start()
    return status()


def start_if_missing() -> None:
    """应用启动时调用：没装过就悄悄下载，不阻塞启动。"""
    if installed_exe() is not None:
        return
    st = status()
    if st["status"] == "running":
        return
    try:
        start()
    except OSError:
        pass
