"""项目文件落盘与路径校验。

所有文件都必须在 ``projects/<projectId>/files/`` 之内；拒绝 ``..``、
绝对路径与符号链接逃逸。
"""

from __future__ import annotations

import re
from pathlib import Path

_UNSAFE = re.compile(r'[<>:"|?*\x00-\x1f]')
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_name(filename: str, fallback: str = "未命名文件") -> str:
    """只留最后一段文件名，并替换非法字符。"""
    name = Path(filename or "").name.strip()
    name = _UNSAFE.sub("_", name).strip(" .")
    if not name:
        return fallback
    stem = Path(name).stem
    if stem.upper() in _WINDOWS_RESERVED:
        name = f"_{name}"
    return name[:180]


def files_root(workspace: Path) -> Path:
    return (Path(workspace) / "files").resolve()


def resolve_in_files(workspace: Path, relative: str) -> Path:
    """把相对路径解析到 files 目录内；越界直接抛 ValueError。"""
    root = files_root(workspace)
    target = (root / relative).resolve()
    if target != root and root not in target.parents:
        raise ValueError("path escapes project files")
    return target


def save_bytes(workspace: Path, filename: str, data: bytes, parent: str = "") -> tuple[str, int]:
    """写入 ``files/<parent>/<name>``，重名自动加 ``-1`` 后缀。

    返回 ``(相对路径, 字节数)``，相对路径用 ``/`` 分隔，便于前端展示。
    """
    base = resolve_in_files(workspace, parent) if parent else files_root(workspace)
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        # 历史数据里可能残留 Windows 非法目录名，把它当参数错误而不是 500。
        raise ValueError(f"invalid folder path: {parent}") from error
    name = safe_name(filename)
    target = base / name
    if target.exists():
        stem, suffix = Path(name).stem, Path(name).suffix
        for index in range(1, 1000):
            candidate = base / f"{stem}-{index}{suffix}"
            if not candidate.exists():
                target = candidate
                break
    target.write_bytes(data)
    relative = target.relative_to(files_root(workspace)).as_posix()
    return relative, len(data)


def remove_file(workspace: Path, relative: str) -> None:
    if not relative:
        return
    try:
        target = resolve_in_files(workspace, relative)
    except ValueError:
        return
    target.unlink(missing_ok=True)
