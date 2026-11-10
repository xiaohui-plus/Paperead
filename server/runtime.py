"""运行环境探测：frozen 路径、数据目录、token、允许的 Origin。

桌面版由 Tauri 壳传入 ``PAPEREAD_PORT`` / ``PAPEREAD_DATA`` /
``PAPEREAD_TOKEN``；直接 ``python -m server.main`` 时使用仓库内 ``data/``。
"""

from __future__ import annotations

import os
import secrets
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


def default_data_dir() -> Path:
    explicit = read_env("PAPEREAD_DATA")
    if explicit:
        return Path(explicit).expanduser().resolve()
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
