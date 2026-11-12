"""运行环境探测：frozen 路径、数据目录、token、允许的 Origin。

桌面版由 Tauri 壳传入 ``PAPEREAD_PORT`` / ``PAPEREAD_DATA`` /
``PAPEREAD_TOKEN``；直接 ``python -m server.main`` 时使用仓库内 ``data/``。
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import sys
from pathlib import Path

from paperead.env import env as read_env, migrate_legacy_dir

DATA_DIR_NAME = "paperead-data"
LEGACY_DATA_DIR_NAME = "paper-reflow-data"


def is_frozen() -> bool:
    """是否运行在 PyInstaller 产物里。"""
    return bool(getattr(sys, "frozen", False))


def bundle_root() -> Path:
    """PyInstaller 解压目录（``_MEIPASS``）；开发态为仓库根目录。"""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def web_dist() -> Path:
    return bundle_root() / "web" / "dist"


def _default_data_dir_path() -> Path:
    """系统默认文献库路径，不读自定义配置。"""
    if is_frozen():
        base = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA")
        if base:
            root = Path(base)
            return migrate_legacy_dir(
                root / DATA_DIR_NAME, root / LEGACY_DATA_DIR_NAME
            )
        home = Path.home()
        return migrate_legacy_dir(
            home / f".{DATA_DIR_NAME}", home / f".{LEGACY_DATA_DIR_NAME}"
        )
    return bundle_root() / "data"


def _library_has_data(path: Path) -> bool:
    """目录里是否已有文献库痕迹（用于判断自定义路径是否还能用）。"""
    return (
        (path / "library.json").is_file()
        or (path / "papers").is_dir()
        or (path / "research").is_dir()
    )


def default_data_dir() -> Path:
    explicit = read_env("PAPEREAD_DATA")
    if explicit:
        return Path(explicit).expanduser().resolve()
    configured = load_data_location()
    if configured is not None:
        # 自定义路径还在且有数据：继续用
        if _library_has_data(configured):
            return configured
        # 自定义路径失效（移动硬盘未挂载、目录被改名）：回退默认库，
        # 但不覆盖配置；盘恢复后重启会自动回到自定义路径。
        fallback = _default_data_dir_path()
        if _library_has_data(fallback):
            return fallback
        # 两边都没有数据：保留配置，避免在用户不知情时切到空库
        return configured
    return _default_data_dir_path()


def config_dir() -> Path:
    """应用级配置目录（不随文献库迁移而移动）。"""
    override = os.getenv("PAPEREAD_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.getenv("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.getenv("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "Paperead"


def location_file() -> Path:
    return config_dir() / "data-location.json"


def location_backup_file() -> Path:
    return config_dir() / "data-location.json.bak"


def _read_location_file(file: Path) -> dict:
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def load_location_record() -> dict:
    """读取路径配置；主文件损坏时回退到上一次的备份。"""
    for file in (location_file(), location_backup_file()):
        data = _read_location_file(file)
        if str(data.get("path") or "").strip():
            return data
    return {}


def load_data_location() -> Path | None:
    """用户自定义的文献库路径；没有配置时返回 None。"""
    value = str(load_location_record().get("path") or "").strip()
    return Path(value).expanduser() if value else None


def save_data_location(path: Path, previous: Path | None = None) -> None:
    target = location_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    # 覆盖前保留上一份配置，主文件被写坏/断电时仍能找回保存路径
    if target.is_file():
        try:
            shutil.copy2(target, location_backup_file())
        except OSError:
            pass
    payload: dict = {"path": str(path)}
    if previous is not None:
        payload["previous"] = str(previous)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)


def cleanup_previous_data_dir() -> None:
    """迁移完成并重启后，清掉旧文献库目录。

    迁移请求只复制数据，旧目录要等当前进程释放 SQLite/PDF 句柄后才能删除；
    启动阶段（还没打开任何数据文件）是唯一安全时机。删除失败不影响启动，
    下次启动再试。
    """
    record = load_location_record()
    previous = str(record.get("previous") or "").strip()
    current = str(record.get("path") or "").strip()
    if not previous or not current:
        return
    old = Path(previous)
    try:
        same = old.resolve() == Path(current).resolve()
    except OSError:
        same = False
    if same:
        save_data_location(Path(current))
        return
    if not old.exists():
        save_data_location(Path(current))
        return
    try:
        shutil.rmtree(old)
    except OSError:
        return
    save_data_location(Path(current))


def ensure_data_dir(path: Path) -> Path:
    for child in ("papers", "logs", "backups"):
        (path / child).mkdir(parents=True, exist_ok=True)
    return path


def new_token() -> str:
    """壳侧生成 token；保留此函数便于测试和直接运行时使用。"""
    return secrets.token_urlsafe(32)


# 安卓壳用内置前端时，请求来自 WebView 的本地源；iOS/Capacitor 也一并放行。
MOBILE_ORIGINS = {
    "http://tauri.localhost",
    "https://tauri.localhost",
    "tauri://localhost",
    "capacitor://localhost",
}


def allowed_origins(port: int) -> set[str]:
    """生产同源 + Vite 开发服务器 + 安卓/移动壳。"""
    origins = {
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    } | MOBILE_ORIGINS
    extra = read_env("PAPEREAD_ALLOW_ORIGINS")
    if extra:
        origins |= {item.strip().rstrip("/") for item in extra.split(",") if item.strip()}
    return origins


def bind_host() -> str:
    """监听地址：默认回环；桌面壳为设备同步会设为 0.0.0.0，独立部署同理。"""
    return read_env("PAPEREAD_HOST", "127.0.0.1")
