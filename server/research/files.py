"""项目文件落盘与路径校验。

默认文件都必须在 ``projects/<projectId>/files/`` 之内；项目绑定外部
工作文件夹后，文件根目录换成用户选择的本地文件夹。两种模式都拒绝
``..``、越界绝对路径与符号链接逃逸。
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


def resolve_in_root(root: Path, relative: str) -> Path:
    """把相对路径解析到任意根目录内；越界直接抛 ValueError。"""
    base = Path(root).resolve()
    target = (base / relative).resolve()
    if target != base and base not in target.parents:
        raise ValueError("path escapes project root")
    return target


def resolve_in_files(workspace: Path, relative: str) -> Path:
    """把相对路径解析到 files 目录内；越界直接抛 ValueError。"""
    return resolve_in_root(files_root(workspace), relative)


def save_bytes_in(
    root: Path, filename: str, data: bytes, parent: str = ""
) -> tuple[str, int]:
    """写入 ``<root>/<parent>/<name>``，重名自动加 ``-1`` 后缀。"""
    base = resolve_in_root(root, parent) if parent else Path(root).resolve()
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as error:
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
    relative = target.relative_to(Path(root).resolve()).as_posix()
    return relative, len(data)


def save_bytes(workspace: Path, filename: str, data: bytes, parent: str = "") -> tuple[str, int]:
    """写入 ``files/<parent>/<name>``，重名自动加 ``-1`` 后缀。

    返回 ``(相对路径, 字节数)``，相对路径用 ``/`` 分隔，便于前端展示。
    """
    return save_bytes_in(files_root(workspace), filename, data, parent)


def remove_file(workspace: Path, relative: str) -> None:
    remove_file_in(files_root(workspace), relative)


def remove_file_in(root: Path, relative: str) -> None:
    if not relative:
        return
    try:
        target = resolve_in_root(root, relative)
    except ValueError:
        return
    target.unlink(missing_ok=True)