"""本机文件夹浏览：Jupyter 式目录列表与项目工作文件夹内的相对浏览。

列目录只读；工作文件夹内的重命名、复制、移动、删除也只允许在项目根内。
"""

from __future__ import annotations

import mimetypes
import os
import shutil
import string
from pathlib import Path
from typing import Any

from server.research.files import resolve_in_root, safe_name

MAX_ENTRIES = 2000
AGENT_MAX_FILES = 300
AGENT_MAX_DEPTH = 3
SKIP_NAMES = {
    "$recycle.bin",
    "system volume information",
    "pagefile.sys",
    "hiberfil.sys",
    "swapfile.sys",
    "node_modules",
    ".git",
    "__pycache__",
}


def _is_skipped(name: str) -> bool:
    return name.lower() in SKIP_NAMES or name.startswith(".~")


def _windows_drives() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for letter in string.ascii_uppercase:
        drive = Path(f"{letter}:\\")
        try:
            if drive.exists():
                items.append(_dir_entry(drive, str(drive)))
        except OSError:
            continue
    return items


def _dir_entry(path: Path, display: str) -> dict[str, Any]:
    modified = 0
    try:
        modified = int(path.stat().st_mtime)
    except OSError:
        pass
    return {
        "name": display,
        "path": display,
        "absolute": str(path),
        "isDir": True,
        "size": 0,
        "modifiedAt": modified,
        "kind": "folder",
    }


def _entry(path: Path, root: Path | None = None) -> dict[str, Any] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    is_dir = path.is_dir()
    if root is not None:
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            relative = path.name
        display = relative
    else:
        relative = str(path)
        display = str(path)
    mime = "" if is_dir else (mimetypes.guess_type(path.name)[0] or "")
    return {
        "name": path.name,
        "path": relative,
        "absolute": str(path),
        "isDir": is_dir,
        "size": 0 if is_dir else int(stat.st_size),
        "modifiedAt": int(stat.st_mtime),
        "kind": "folder" if is_dir else "file",
        "mime": mime,
    }


def _sort(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items.sort(key=lambda item: (not item["isDir"], item["name"].casefold()))
    return items


def _list_children(directory: Path, root: Path | None = None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    try:
        entries = list(os.scandir(directory))
    except OSError as error:
        raise PermissionError(f"无法读取文件夹：{error}") from error
    for entry in entries:
        if len(items) >= MAX_ENTRIES:
            break
        name = entry.name
        if root is not None and _is_skipped(name):
            continue
        try:
            path = Path(entry.path)
        except OSError:
            continue
        item = _entry(path, root)
        if item is None:
            continue
        if item["isDir"]:
            item["mime"] = ""
        items.append(item)
    return _sort(items)


def quick_roots() -> list[dict[str, Any]]:
    roots: list[dict[str, Any]] = []
    if os.name == "nt":
        roots.extend(_windows_drives())
    else:
        roots.append(_dir_entry(Path("/"), "/"))

    home = Path.home()
    candidates = [
        ("主目录", home),
        ("桌面", home / "Desktop"),
        ("文档", home / "Documents"),
        ("下载", home / "Downloads"),
    ]
    seen = {str(Path(item["absolute"]).resolve()).lower() for item in roots}
    for label, path in candidates:
        try:
            if not path.is_dir():
                continue
            key = str(path.resolve()).lower()
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        item = _dir_entry(path, label)
        item["name"] = label
        item["path"] = str(path.resolve())
        item["absolute"] = str(path.resolve())
        roots.append(item)
    return roots


def browse(path: str | None) -> dict[str, Any]:
    """浏览本机任意文件夹；空路径返回盘符/常用位置。"""
    raw = (path or "").strip()
    if not raw:
        return {"path": "", "parent": None, "items": quick_roots()}
    candidate = Path(raw).expanduser()
    try:
        directory = candidate.resolve(strict=True)
    except OSError as error:
        raise FileNotFoundError(f"文件夹不存在：{raw}") from error
    if not directory.is_dir():
        raise NotADirectoryError("不是文件夹")
    parent = directory.parent
    return {
        "path": str(directory),
        "parent": "" if parent == directory else str(parent),
        "items": _list_children(directory),
    }


def list_project(root: Path, relative: str | None) -> dict[str, Any]:
    """列出项目工作文件夹内某个相对目录。"""
    base = Path(root).resolve()
    target = resolve_in_root(base, relative or ".")
    if not target.is_dir():
        raise NotADirectoryError("不是文件夹")
    parent = ""
    if target != base:
        parent = target.parent.relative_to(base).as_posix()
        if parent == ".":
            parent = ""
    return {
        "root": str(base),
        "path": "" if target == base else target.relative_to(base).as_posix(),
        "parent": parent,
        "items": _list_children(target, base),
    }


def project_file(root: Path, relative: str) -> Path:
    base = Path(root).resolve()
    target = resolve_in_root(base, relative)
    if not target.is_file():
        raise FileNotFoundError("文件不存在")
    return target


def _relative(base: Path, path: Path) -> str:
    return path.relative_to(base).as_posix()


def _unique_child(directory: Path, name: str) -> Path:
    """目标目录里已有同名项时，按 ``-1``、``-2`` 找一个可用名。"""
    target = directory / name
    if not target.exists():
        return target
    stem, suffix = Path(name).stem, Path(name).suffix
    for index in range(1, 1000):
        candidate = directory / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise FileExistsError("目标文件夹里同名项目过多")


def rename(root: Path, relative: str, name: str) -> str:
    """重命名工作文件夹内的文件或文件夹，返回新的相对路径。"""
    base = Path(root).resolve()
    source = resolve_in_root(base, relative)
    if source == base:
        raise ValueError("不能重命名项目文件夹")
    if not source.exists():
        raise FileNotFoundError("文件不存在")
    clean = safe_name(name)
    if clean == source.name:
        return _relative(base, source)
    target = source.with_name(clean)
    if target.exists():
        raise FileExistsError("同名文件已存在")
    source.rename(target)
    return _relative(base, target)


def transfer(root: Path, relative: str, target_dir: str = "", mode: str = "copy") -> str:
    """在工作文件夹内复制或移动文件，返回目标相对路径。"""
    base = Path(root).resolve()
    source = resolve_in_root(base, relative)
    if source == base:
        raise ValueError("不能复制或移动项目文件夹")
    if not source.exists():
        raise FileNotFoundError("文件不存在")
    directory = resolve_in_root(base, target_dir) if target_dir else base
    if not directory.is_dir():
        raise NotADirectoryError("目标不是文件夹")
    if directory == source or source in directory.parents:
        raise ValueError("不能放进自身或子文件夹")
    if mode == "move" and directory == source.parent:
        return _relative(base, source)
    destination = _unique_child(directory, source.name)
    if mode == "move":
        shutil.move(str(source), str(destination))
    elif mode == "copy":
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)
    else:
        raise ValueError("无效操作")
    return _relative(base, destination)


def delete(root: Path, relative: str) -> None:
    """删除工作文件夹内的文件或整个文件夹。"""
    base = Path(root).resolve()
    target = resolve_in_root(base, relative)
    if target == base:
        raise ValueError("不能删除项目文件夹")
    if not target.exists():
        raise FileNotFoundError("文件不存在")
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()


def project_files(root: Path, limit: int = AGENT_MAX_FILES) -> list[dict[str, Any]]:
    """给 agent 的项目文件清单：相对路径、类型、大小，限制深度与数量。"""
    base = Path(root).resolve()
    results: list[dict[str, Any]] = []
    if not base.is_dir():
        return results
    stack: list[tuple[Path, int]] = [(base, 0)]
    while stack and len(results) < limit:
        directory, depth = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in sorted(entries, key=lambda item: item.name.casefold()):
            if len(results) >= limit:
                break
            name = entry.name
            if _is_skipped(name):
                continue
            try:
                path = Path(entry.path)
                relative = path.relative_to(base).as_posix()
                is_dir = entry.is_dir(follow_symlinks=False)
                size = 0 if is_dir else entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
            results.append(
                {
                    "name": path.name,
                    "path": relative,
                    "kind": "folder" if is_dir else "file",
                    "size": int(size),
                }
            )
            if is_dir and depth + 1 < AGENT_MAX_DEPTH:
                stack.append((path, depth + 1))
    results.sort(
        key=lambda item: (item["kind"] != "folder", item["name"].casefold())
    )
    return results