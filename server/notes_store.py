"""外部论文笔记的本地存储。

与 PDF 论文完全分开：笔记没有 PDF、没有页码坐标，塞进文献库会污染
`library.json` 与论文检索。这里单独落 `data/notes/<id>.json`。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any

from paperead.notes_import import NOTE_LICENSE, NOTE_ORIGIN, NOTE_SOURCE, Note


class NoteStore:
    def __init__(self, root: Path):
        self.root = Path(root) / "notes"
        self._lock = threading.Lock()

    def note_id(self, source_path: str) -> str:
        digest = hashlib.sha1(source_path.encode("utf-8")).hexdigest()
        return f"n_{digest[:12]}"

    def note_path(self, note_id: str) -> Path:
        return self.root / f"{note_id}.json"

    def _read(self, path: Path) -> dict[str, Any] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def get(self, note_id: str) -> dict[str, Any] | None:
        if not note_id.startswith("n_"):
            return None
        return self._read(self.note_path(note_id))

    def list(self) -> list[dict[str, Any]]:
        """只返回元信息，不带 blocks（列表页不需要正文）。"""
        if not self.root.exists():
            return []
        rows: list[dict[str, Any]] = []
        for path in self.root.glob("n_*.json"):
            data = self._read(path)
            if not data:
                continue
            rows.append({key: value for key, value in data.items() if key != "blocks"})
        rows.sort(key=lambda row: (row.get("conference") or "", row.get("title") or ""))
        return rows

    def upsert(self, note: Note) -> dict[str, Any]:
        payload = note.to_dict()
        note_id = self.note_id(payload["source_path"])
        payload.update(
            {
                "id": note_id,
                "source": NOTE_SOURCE,
                "license": NOTE_LICENSE,
                "origin": NOTE_ORIGIN,
                "imported_at": time.time(),
            }
        )
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            path = self.note_path(note_id)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            tmp.replace(path)
        return {key: value for key, value in payload.items() if key != "blocks"}

    def count(self) -> int:
        return len(list(self.root.glob("n_*.json"))) if self.root.exists() else 0
    def link_paper(self, note_id: str, paper_id: str) -> dict[str, Any] | None:
        """把笔记与其在文献库里的论文关联，供详情页「已在文献库」直接跳转。"""
        with self._lock:
            path = self.note_path(note_id)
            data = self._read(path)
            if data is None:
                return None
            data["paper_id"] = paper_id
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            tmp.replace(path)
        return {key: value for key, value in data.items() if key != "blocks"}