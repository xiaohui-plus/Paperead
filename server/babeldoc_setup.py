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
from typing import Any, Callable

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
    "phase": "",  # manifest | download | verify | extract | done
    "percent": 0,
    "bytes": 0,
    "total": 0,
    "source": "",
    "attempt": 0,
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
    """当前 App 是否已有可直接使用的引擎。

    先认期望版本目录；其余版本目录也认——历史清单版本号与 ``ENGINE_VERSION``
    不一致时，旧目录其实完全能跑。若只认精确目录，会出现「已下载到 100%、
    但每次启动仍判定未就绪并重复下载」的死循环，用户看到的就是「显示装好了
    却始终无法完成依赖」。
    """
    explicit = os.environ.get("PAPEREAD_BABELDOC_HOME", "").strip()
    if explicit:
        return babeldoc_engine.launcher(Path(explicit).expanduser()) is not None
    if babeldoc_engine.launcher(install_dir()) is not None:
        return True
    root = babeldoc_engine.engine_root()
    if root.is_dir():
        try:
            children = sorted(root.iterdir(), reverse=True)
        except OSError:
            children = []
        for child in children:
            if child.is_dir() and babeldoc_engine.launcher(child) is not None:
                return True
    return False


def installed_version() -> str:
    """已安装引擎的目录名（版本）；没有则返回空串。"""
    explicit = os.environ.get("PAPEREAD_BABELDOC_HOME", "").strip()
    if explicit:
        home = Path(explicit).expanduser()
        return home.name if babeldoc_engine.launcher(home) is not None else ""
    if babeldoc_engine.launcher(install_dir()) is not None:
        return ENGINE_VERSION
    root = babeldoc_engine.engine_root()
    if root.is_dir():
        try:
            children = sorted(root.iterdir(), reverse=True)
        except OSError:
            children = []
        for child in children:
            if child.is_dir() and babeldoc_engine.launcher(child) is not None:
                return child.name
    return ""


def _legacy_manifest_urls() -> list[str]:
    """改造前的地址：依赖仓库未配置或尚未发布时兜底。"""
    from paperead import __version__

    return [
        f"https://gitee.com/deng_xiaohui/paperead/releases/download/latest/{MANIFEST_NAME}",
        f"https://gitee.com/deng_xiaohui/paperead/releases/download/v{__version__}/{MANIFEST_NAME}",
        f"https://ghproxy.net/https://github.com/xiaohui-plus/Paperead/releases/latest/download/{MANIFEST_NAME}",
        f"https://github.com/xiaohui-plus/Paperead/releases/latest/download/{MANIFEST_NAME}",
    ]


def _manifest_sources() -> list[str]:
    override = os.environ.get("PAPEREAD_BABELDOC_ENGINE_URL", "").strip()
    if override:
        return [override]
    from server import deps_source

    return deps_source.release_urls(MANIFEST_NAME, extra=_legacy_manifest_urls())


def _download_bases(primary: str) -> list[str]:
    """分卷下载的候选目录（按优先级）。

    清单与分卷可能来自不同渠道：依赖仓库正常时优先用它，迁移期或依赖仓库
    尚未发布时回落到主仓库的旧目录。同一份清单（版本 + SHA-256 校验）允许
    跨源组合分卷，所以主源 404 时自动换下一个。
    """
    # 显式指定清单地址（内网/测试）时，分卷只认该源的目录，不回落公网。
    if os.environ.get("PAPEREAD_BABELDOC_ENGINE_URL", "").strip():
        return [primary]

    from paperead import __version__
    from server import deps_source

    legacy = [
        f"https://gitee.com/deng_xiaohui/paperead/releases/download/v{__version__}",
        "https://gitee.com/deng_xiaohui/paperead/releases/download/latest",
        "https://ghproxy.net/https://github.com/xiaohui-plus/Paperead/releases/latest/download",
        "https://github.com/xiaohui-plus/Paperead/releases/latest/download",
    ]
    return deps_source.merged_bases(primary, extra=legacy)


def status() -> dict[str, Any]:
    with _LOCK:
        data = dict(_STATE)
    # ``installed`` 表示「任意版本可用」，``ready`` 表示「当前期望版本或任一
    # 可用引擎已就位」；``installedVersion`` 与 ``expectedVersion`` 不同时，前端
    # 可以提示「可升级到新版本」，但翻译入口仍可继续使用旧引擎。
    data["installed"] = installed()
    data["ready"] = _ready()
    data["version"] = ENGINE_VERSION
    data["expectedVersion"] = ENGINE_VERSION
    data["installedVersion"] = installed_version()
    data["upgradeAvailable"] = bool(
        data["installedVersion"] and data["installedVersion"] != ENGINE_VERSION
    )
    home = babeldoc_engine.engine_home()
    data["home"] = str(home or install_dir())
    try:
        data["cache_ready"] = bool(babeldoc_engine.status().get("cache_ready"))
    except Exception:  # noqa: BLE001 - 缓存探测失败不影响引擎本身可用
        data["cache_ready"] = False
    alive = _THREAD is not None and _THREAD.is_alive()
    data["threadAlive"] = alive
    # 线程已退出但状态还停在 running（异常逃逸/进程被挂起）时不能让界面永远转圈。
    if data["status"] == "running" and not alive:
        data["status"] = "error"
        data["error"] = data.get("error") or "下载线程已退出，请重试。"
    return data


def _set(**fields: Any) -> None:
    with _LOCK:
        _STATE.update(fields)


def _fetch_json(url: str, timeout: int = 30) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "paperead"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content_type = (response.headers.get("Content-Type") or "").lower()
        raw = response.read()
    if "html" in content_type:
        # Gitee 空仓库 / 尚未发布发行版时，附件地址会 302 到仓库首页；
        # 不识别的话只会报 JSON 解析失败，看不出真实原因。
        raise ValueError("下载源返回网页而不是清单（依赖仓库为空或发行版未发布）")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("清单格式不正确")
    message = str(payload.get("message") or "").strip()
    if message and not payload.get("version"):
        # Gitee 空仓库 / 不存在的附件也可能返回 200 + {"message": "..."}。
        raise ValueError(f"下载源返回错误：{message}")
    return payload


def _download(url: str, dest: Path, on_bytes) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "paperead"})
    with urllib.request.urlopen(request, timeout=60) as response:
        content_type = (response.headers.get("Content-Type") or "").lower()
        if content_type.startswith("application/json"):
            detail = response.read(400).decode("utf-8", errors="replace")
            raise ValueError(f"下载源返回错误：{detail.strip()}")
        if "html" in content_type:
            raise ValueError("下载源返回网页而不是文件（依赖仓库为空或附件不存在）")
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


def _safe_extract(
    archive: Path,
    target: Path,
    on_progress: Callable[[int, int], None] | None = None,
) -> None:
    target.mkdir(parents=True, exist_ok=True)
    root = target.resolve()
    with zipfile.ZipFile(archive) as bundle:
        infos = bundle.infolist()
        for info in infos:
            dest = (target / info.filename).resolve()
            if dest != root and root not in dest.parents:
                raise ValueError(f"压缩包路径非法：{info.filename}")
        total = max(1, len(infos))
        for index, info in enumerate(infos, 1):
            bundle.extract(info, target)
            if on_progress is not None:
                on_progress(index, total)


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
                _set(
                    phase="download",
                    percent=min(88, 2 + int(downloaded * 86 / expected_size)),
                    bytes=downloaded,
                    total=expected_size,
                )
            else:
                _set(phase="download", bytes=downloaded)

        archive = work / ZIP_NAME
        with archive.open("wb") as handle:
            for name in parts:
                temp = work / f"{name}.part"
                ok = False
                last_error: Exception | None = None
                for base in _download_bases(base_url):
                    # 失败换源时回滚进度，避免同分卷重复计数把百分比顶满。
                    mark = downloaded
                    url = f"{base}/{urllib.parse.quote(name)}"
                    _set(source=urllib.parse.urlparse(base).netloc or base)
                    try:
                        _download(url, temp, on_bytes)
                        ok = True
                        break
                    except (OSError, ValueError, urllib.error.URLError) as error:
                        last_error = error
                        downloaded = mark
                        temp.unlink(missing_ok=True)
                        log.warning("分卷 %s 从 %s 下载失败：%s", name, base, error)
                if not ok:
                    raise ValueError(f"分卷 {name} 下载失败：{last_error}")
                with temp.open("rb") as source:
                    shutil.copyfileobj(source, handle, CHUNK)
                temp.unlink(missing_ok=True)

        if expected_size and archive.stat().st_size != expected_size:
            raise ValueError("下载体积与清单不一致")
        _set(phase="verify", percent=90, bytes=expected_size or downloaded, total=expected_size)
        if _sha256(archive) != expected_sha:
            raise ValueError("下载内容校验失败，请重试")

        _set(phase="extract", percent=93)
        stage = work / "tree"

        def on_extract(done: int, total: int) -> None:
            _set(percent=93 + int(done * 6 / max(1, total)))

        _safe_extract(archive, stage, on_extract)
        if babeldoc_engine.launcher(stage) is None:
            raise ValueError("压缩包里没有可用的 BabelDOC 启动文件")
        _set(percent=99)

        # 统一落到当前期望版本目录：清单版本号与 ENGINE_VERSION 漂移时，
        # 若按清单版本命名，下次启动会判「未就绪」并重复下载整份引擎。
        if version != ENGINE_VERSION:
            log.warning(
                "引擎清单版本 %s 与期望版本 %s 不一致，按期望版本目录安装",
                version,
                ENGINE_VERSION,
            )
        target = install_dir()
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            # 另一个进程已经装好了：直接用现成的；目录在但启动文件缺失说明上次
            # 解压不完整，删掉重装，别让「已安装」状态永远修不好。
            if babeldoc_engine.launcher(target) is not None:
                return target
            shutil.rmtree(target, ignore_errors=True)
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
    attempt = 0
    for manifest_url in _manifest_sources():
        attempt += 1
        host = urllib.parse.urlparse(manifest_url).netloc or manifest_url
        try:
            with _LOCK:
                current_percent = int(_STATE.get("percent") or 0)
            # 清单阶段不清零已下载进度：换清单源时界面不会突然从 60% 掉回 0%。
            _set(phase="manifest", attempt=attempt, source=host, error="")
            if current_percent <= 0:
                _set(percent=0, bytes=0, total=0)
            manifest = _fetch_json(manifest_url)
            base_url = manifest_url.rsplit("/", 1)[0]
            _install(manifest, base_url)
            with _LOCK:
                total = int(_STATE.get("total") or 0)
            _set(
                status="done",
                phase="done",
                percent=100,
                bytes=total,
                finishedAt=int(time.time()),
            )
            return
        except (
            OSError,
            ValueError,
            zipfile.BadZipFile,
            urllib.error.URLError,
            json.JSONDecodeError,
        ) as error:
            errors.append(f"{host}: {error}")
            log.warning("BabelDOC 引擎下载失败（%s）：%s", host, error)
            # 失败后把当前源写进状态：前端能显示「正在切换备用源」，而不是
            # 一直停在 0% 让人以为卡死。
            _set(
                phase="manifest",
                source=host,
                error=f"{host}：{error}，正在尝试备用下载源…",
            )
    _set(
        status="error",
        phase="error",
        error="；".join(errors)[-800:] or "下载失败",
        finishedAt=int(time.time()),
    )


def start(force: bool = False) -> dict[str, Any]:
    """开始后台下载；已有可用引擎或正在下载时直接返回当前状态。"""
    global _THREAD
    with _LOCK:
        if not force and _ready():
            _STATE.update(status="done", phase="done", percent=100, error="")
            return dict(_STATE)
        if _THREAD is not None and _THREAD.is_alive():
            return dict(_STATE)
        if _STATE.get("status") == "running":
            # 线程已退出但状态仍停在 running：重置后允许重新开始。
            _STATE.update(status="error", error="下载线程已退出，请重试。", phase="error")
        _STATE.update(
            status="running",
            phase="manifest",
            percent=0,
            bytes=0,
            total=0,
            source="",
            attempt=0,
            error="",
            startedAt=int(time.time()),
            finishedAt=0,
        )
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
