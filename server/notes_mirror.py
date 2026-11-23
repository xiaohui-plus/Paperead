"""在线笔记的本地镜像。

镜像包按会议分卷（`docs/<会议>/...` 的 tar.gz），解压到 `data/notes-mirror/docs/`。
启动时拉固定清单，比对 sha256：缺失或变化的会议才下载，校验通过后整体替换，
读取时优先本地文件，缺失才回退在线源。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tarfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from server.notes_http import http_download, http_get_text

CONF_RE = re.compile(r"^[A-Z]{2,8}\d{4}$")

MIRROR_MANIFEST_URL = (
    "https://gitee.com/deng_xiaohui/paperead/raw/master/mirror-manifest.json"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class NotesMirror:
    def __init__(self, data_dir: Path):
        self.root = Path(data_dir) / "notes-mirror"
        self.state_path = self.root / "state.json"
        self._lock = threading.Lock()
        self._cache: dict[str, Any] | None = None
        self._state = self._load_state()
        self._sync_state: dict[str, Any] = {
            "state": "idle",
            "version": self._state.get("version", ""),
            "updated": [],
            "skipped": [],
            "failed": [],
            "pending": [],
            "error": "",
        }
        self._sync_thread: threading.Thread | None = None

    # ── 状态 ────────────────────────────────────────────

    def _load_state(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"version": "", "updated_at": 0, "conferences": {}}
        if not isinstance(data, dict):
            return {"version": "", "updated_at": 0, "conferences": {}}
        data.setdefault("conferences", {})
        return data

    def _save_state(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        tmp.replace(self.state_path)

    def installed(self, refresh: bool = False) -> dict[str, Any]:
        if self._cache is not None and not refresh:
            return self._cache

        conferences: dict[str, Any] = {}
        docs = self.root / "docs"
        if docs.is_dir():
            for folder in sorted(docs.iterdir()):
                if not folder.is_dir() or not CONF_RE.fullmatch(folder.name):
                    continue
                files = list(folder.rglob("*.md"))
                size = sum(path.stat().st_size for path in files)
                conferences[folder.name] = {"notes": len(files), "bytes": size}
        self._cache = {
            "root": str(self.root),
            "conferences": conferences,
            "total_bytes": sum(item["bytes"] for item in conferences.values()),
        }
        return self._cache

    def sync_status(self) -> dict[str, Any]:
        return {**self._sync_state, **self.installed()}

    # ── 读取 ────────────────────────────────────────────

    def note_path(self, rel: str) -> Path | None:
        rel = (rel or "").replace("\\", "/")
        if not rel or rel.startswith("/") or ".." in rel.split("/"):
            return None
        target = self.root / "docs" / rel
        return target if target.is_file() else None

    # ── 同步 ────────────────────────────────────────────

    def auto_sync(
        self, manifest_url: str = MIRROR_MANIFEST_URL, force: bool = False
    ) -> dict[str, Any]:
        """拉固定清单，只更新缺失或哈希变化的会议。"""
        if self._sync_thread is not None and self._sync_thread.is_alive():
            return self.sync_status()

        try:
            manifest = json.loads(http_get_text(manifest_url, timeout=20))
            entries = manifest.get("conferences") or []
            if not entries:
                raise RuntimeError("镜像清单为空")
        except Exception as error:
            self._sync_state = {
                **self._sync_state,
                "state": "error",
                "error": str(error)[:160],
                "pending": [],
            }
            return self.sync_status()

        version = str(manifest.get("version") or "")
        remote = {
            str(item.get("conference")): item
            for item in entries
            if item.get("conference")
        }
        installed = set(self.installed(refresh=True)["conferences"])
        known = self._state.setdefault("conferences", {})

        pending: list[tuple[str, dict[str, Any]]] = []
        skipped: list[str] = []
        migrated = False
        for conference, meta in remote.items():
            local_sha = str((known.get(conference) or {}).get("sha256") or "")
            remote_sha = str(meta.get("sha256") or "")
            if not force and remote_sha and local_sha == remote_sha and conference in installed:
                skipped.append(conference)
                continue
            if not force and conference in installed and not local_sha:
                # 从旧版本升级：本地已有镜像但没有状态，先登记，避免重下几百 MB
                known[conference] = {
                    "sha256": remote_sha,
                    "bytes": meta.get("bytes"),
                    "notes": self._count_notes(conference),
                }
                skipped.append(conference)
                migrated = True
                continue
            pending.append((conference, meta))

        if migrated:
            self._state["version"] = version
            self._state["updated_at"] = time.time()
            self._save_state()

        self._sync_state = {
            "state": "syncing" if pending else "done",
            "version": version,
            "updated": [],
            "skipped": skipped,
            "failed": [],
            "pending": [conference for conference, _ in pending],
            "error": "",
        }
        if not pending:
            return self.sync_status()

        self._sync_thread = threading.Thread(
            target=self._sync_loop, args=(pending, version), daemon=True
        )
        self._sync_thread.start()
        return self.sync_status()

    def _sync_loop(self, pending: list[tuple[str, dict[str, Any]]], version: str) -> None:
        downloads = self.root / ".downloads"

        def one(item: tuple[str, dict[str, Any]]) -> None:
            conference, meta = item
            archive = downloads / f"{conference}.tar.gz"
            try:
                url = str(meta.get("url") or "")
                if not url.startswith(("https://", "http://")):
                    raise RuntimeError("缺少下载地址")
                http_download(url, archive, timeout=900)
                expected = str(meta.get("sha256") or "")
                if expected and _sha256(archive) != expected:
                    raise RuntimeError("sha256 校验失败")
                self._replace_extract(archive, conference)
                with self._lock:
                    self._state.setdefault("conferences", {})[conference] = {
                        "sha256": expected,
                        "bytes": meta.get("bytes"),
                        "notes": self._count_notes(conference),
                    }
                    self._state["updated_at"] = time.time()
                    self._save_state()
                self._sync_state["updated"].append(conference)
            except Exception as error:  # 单会议失败不影响其他会议
                self._sync_state["failed"].append([conference, str(error)[:140]])
            finally:
                archive.unlink(missing_ok=True)

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(one, pending))

        with self._lock:
            self._state["version"] = version
            self._state["updated_at"] = time.time()
            self._save_state()
        self._sync_state["pending"] = []
        self._sync_state["state"] = (
            "done" if not self._sync_state["failed"] else "partial"
        )

    def _count_notes(self, conference: str) -> int:
        folder = self.root / "docs" / conference
        if not folder.is_dir():
            return 0
        return sum(1 for _ in folder.rglob("*.md"))

    def _replace_extract(self, archive: Path, conference: str) -> None:
        """解压到独立目录后整体替换，避免半途失败留下残缺镜像。"""
        if not CONF_RE.fullmatch(conference):
            raise ValueError("会议名不合法")

        staging = self.root / ".staging" / f"{conference}-{uuid.uuid4().hex[:8]}"
        docs = staging / "docs"
        docs.mkdir(parents=True, exist_ok=True)

        with tarfile.open(archive, "r:*") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                parts = [part for part in member.name.replace("\\", "/").split("/") if part]
                if "docs" not in parts:
                    continue
                rel = parts[parts.index("docs") + 1 :]
                if not rel or rel[0] != conference or any(p == ".." for p in rel):
                    continue
                member.name = "/".join(rel)
                tar.extract(member, path=docs, filter="data")

        target = self.root / "docs" / conference
        retired = self.root / ".staging" / f"{conference}.retired"
        if retired.exists():
            shutil.rmtree(retired, ignore_errors=True)
        if target.exists():
            target.replace(retired)
        (docs / conference).replace(target)

        shutil.rmtree(staging, ignore_errors=True)
        if retired.exists():
            shutil.rmtree(retired, ignore_errors=True)
        self._cache = None

    # ── 手动安装 ────────────────────────────────────────

    def extract(self, archive: Path, conference: str) -> dict[str, Any]:
        if not CONF_RE.fullmatch(conference):
            raise ValueError("会议名不合法")
        dest = self.root / "docs"
        dest.mkdir(parents=True, exist_ok=True)

        with self._lock:
            with tarfile.open(archive, "r:*") as tar:
                for member in tar.getmembers():
                    if not member.isfile():
                        continue
                    parts = [part for part in member.name.replace("\\", "/").split("/") if part]
                    if "docs" not in parts:
                        continue
                    rel = parts[parts.index("docs") + 1 :]
                    if not rel or rel[0] != conference or any(p == ".." for p in rel):
                        continue
                    member.name = "/".join(rel)
                    tar.extract(member, path=dest, filter="data")
            self._cache = None
        return self.installed(refresh=True)