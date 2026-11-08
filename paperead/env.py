"""环境变量与旧版本目录兼容。

项目原名 ``paper-reflow``（0.1.0 及更早），改名 Paperead 后仍要照顾已经
写好的 ``.env``、快捷方式、便携版批处理：配置优先读 ``PAPEREAD_*``，
读不到时回退到旧的 ``PAPER_REFLOW_*``。
"""

from __future__ import annotations

import os
from pathlib import Path

PREFIX = "PAPEREAD_"
LEGACY_PREFIX = "PAPER_REFLOW_"


def env(name: str, default: str | None = None) -> str | None:
    """读环境变量：先 ``name``，再看同名旧前缀变量，最后 ``default``。"""
    value = os.getenv(name)
    if value is not None:
        return value
    if name.startswith(PREFIX):
        value = os.getenv(LEGACY_PREFIX + name[len(PREFIX):])
        if value is not None:
            return value
    return default


def migrate_legacy_dir(target: Path, legacy: Path) -> Path:
    """把旧版本目录改名为新目录；返回实际应该使用的目录。

    新目录已存在时直接用它；改名失败（文件被占用、权限不足）时退回
    旧目录继续使用，避免老用户的文献库突然“消失”。
    """
    if target.exists() or not legacy.is_dir():
        return target
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        legacy.rename(target)
        return target
    except OSError:
        return legacy
