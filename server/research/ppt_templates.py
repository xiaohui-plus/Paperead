"""PPT 汇报模板：内置学术模板 + 用户上传，导出 PPT 时默认套用。

存储与论文模板共用 ``data/review-templates/``：

    settings.json         # 同时保存 {"selected": "...", "ppt_selected": "..."}
    ppt-uploads/<名字>.pptx

选择值：
    builtin:academic | builtin:seminar | builtin:defense
    upload:<文件名>.pptx
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from pptx import Presentation

from server.research.files import safe_name

DIR_NAME = "review-templates"
SETTINGS_NAME = "settings.json"
PPT_UPLOADS_DIR = "ppt-uploads"
ASSETS_DIR = Path(__file__).resolve().parent / "ppt_templates"
MAX_PPT_BYTES = 20 * 1024 * 1024
ALLOWED_SUFFIXES = {".pptx", ".potx"}

BUILTIN_TEMPLATES: list[dict[str, str]] = [
    {
        "id": "builtin:academic",
        "name": "学术汇报",
        "description": "深蓝封面与标题条，适合学术会议、组会汇报",
        "file": "academic.pptx",
    },
    {
        "id": "builtin:seminar",
        "name": "组会汇报",
        "description": "墨绿细线，简洁克制，正文信息密度高",
        "file": "seminar.pptx",
    },
    {
        "id": "builtin:defense",
        "name": "学位答辩",
        "description": "黑白留白，适合答辩与正式评审",
        "file": "defense.pptx",
    },
]
DEFAULT_ID = "builtin:academic"


class PptTemplateError(ValueError):
    """PPT 模板不可用（类型、大小、文件损坏或不存在）。"""


def root_for(research_root: Path) -> Path:
    return Path(research_root).parent / DIR_NAME


def uploads_dir(root: Path) -> Path:
    path = Path(root) / PPT_UPLOADS_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def _settings_path(root: Path) -> Path:
    return Path(root) / SETTINGS_NAME


def _read_settings(root: Path) -> dict[str, Any]:
    try:
        payload = json.loads(_settings_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_settings(root: Path, payload: dict[str, Any]) -> None:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    _settings_path(root).write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def read_choice(root: Path) -> str:
    value = str(_read_settings(root).get("ppt_selected") or "").strip()
    return value or DEFAULT_ID


def write_choice(root: Path, value: str) -> None:
    payload = _read_settings(root)
    value = (value or "").strip()
    if value:
        payload["ppt_selected"] = value
    else:
        payload.pop("ppt_selected", None)
    _write_settings(root, payload)


def _upload_paths(root: Path) -> list[Path]:
    folder = Path(root) / PPT_UPLOADS_DIR
    if not folder.is_dir():
        return []
    return sorted(
        (
            path
            for path in folder.iterdir()
            if path.is_file() and path.suffix.lower() in ALLOWED_SUFFIXES
        ),
        key=lambda path: path.name.lower(),
    )


def list_uploads(root: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for path in _upload_paths(root):
        items.append(
            {
                "id": f"upload:{path.name}",
                "name": path.stem,
                "description": "我上传的模板",
                "source": "upload",
                "size": path.stat().st_size,
            }
        )
    return items


def list_items(root: Path) -> list[dict[str, Any]]:
    selected = read_choice(root)
    items: list[dict[str, Any]] = []
    for item in BUILTIN_TEMPLATES:
        items.append(
            {
                "id": item["id"],
                "name": item["name"],
                "description": item["description"],
                "source": "builtin",
                "size": (ASSETS_DIR / item["file"]).stat().st_size
                if (ASSETS_DIR / item["file"]).is_file()
                else 0,
                "selected": item["id"] == selected,
            }
        )
    for row in list_uploads(root):
        items.append({**row, "selected": row["id"] == selected})
    return items


def _builtin_path(choice: str) -> Path | None:
    for item in BUILTIN_TEMPLATES:
        if item["id"] == choice:
            path = ASSETS_DIR / item["file"]
            return path if path.is_file() else None
    return None


def resolve(root: Path, choice: str = "") -> tuple[bytes | None, str]:
    """返回（模板字节, 展示标签）；找不到时回退为默认样式。"""
    value = (choice or read_choice(root)).strip()
    path = _builtin_path(value)
    if path is not None:
        name = next(
            item["name"] for item in BUILTIN_TEMPLATES if item["id"] == value
        )
        return path.read_bytes(), f"内置：{name}"
    if value.startswith("upload:"):
        name = safe_name(value[7:], fallback="")
        candidate = uploads_dir(root) / name
        if candidate.is_file() and candidate.suffix.lower() in ALLOWED_SUFFIXES:
            return candidate.read_bytes(), f"上传：{candidate.stem}"
    return None, "builtin"


def _validate_pptx(data: bytes) -> None:
    try:
        deck = Presentation(io.BytesIO(data))
    except Exception as error:  # noqa: BLE001 - 坏文件要给出明确原因
        raise PptTemplateError("这个文件不是有效的 PowerPoint 模板") from error
    if not deck.slides:
        raise PptTemplateError("模板里至少需要一页幻灯片")


def save_upload(root: Path, filename: str, data: bytes) -> dict[str, Any]:
    suffix = Path(filename or "").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise PptTemplateError("只支持 PowerPoint 模板（.pptx / .potx）")
    if not data:
        raise PptTemplateError("模板文件为空")
    if len(data) > MAX_PPT_BYTES:
        raise PptTemplateError("模板不能超过 20 MB")
    _validate_pptx(data)
    name = safe_name(filename, fallback="汇报模板.pptx")
    if Path(name).suffix.lower() not in ALLOWED_SUFFIXES:
        name = f"{name}.pptx"
    target = uploads_dir(root) / name
    target.write_bytes(data)
    write_choice(root, f"upload:{name}")
    return {"id": f"upload:{name}", "name": target.stem, "source": "upload", "size": len(data)}


def delete_upload(root: Path, name: str) -> bool:
    safe = safe_name(name, fallback="")
    if not safe:
        return False
    target = uploads_dir(root) / safe
    if not target.is_file():
        return False
    was_selected = read_choice(root) == f"upload:{safe}"
    target.unlink()
    if was_selected:
        write_choice(root, DEFAULT_ID)
    return True