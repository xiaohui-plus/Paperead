"""问答记忆：对话流、论文记忆卡、研究者画像、概念台账。

设计见 docs/superpowers/specs/2026-11-08-qa-memory-design.md。
读路径不调用模型；写路径在回答返回后异步跑一次小调用。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

VERSION = 1

TRAIT_KEYS = (
    "domain_background",
    "depth",
    "answer_style",
    "language",
    "goal",
    "math_comfort",
)
TRAIT_LABELS = {
    "domain_background": "背景",
    "depth": "理解深度",
    "answer_style": "答法偏好",
    "language": "语言习惯",
    "goal": "目标",
    "math_comfort": "数学耐受",
}
STATE_RANK = {"confused": 0, "learning": 1, "understood": 2}
STATE_LABELS = {"understood": "已懂", "learning": "在学", "confused": "困惑"}
FACT_KINDS = {"user_stance", "conclusion", "open_question"}
FACT_LABELS = {
    "user_stance": "你的判断",
    "conclusion": "已确认",
    "open_question": "待解决",
}
MAX_FACTS = 12
INDEX_WINDOW = 400


def enabled() -> bool:
    return os.getenv("PAPER_REFLOW_MEMORY", "1") != "0"


def _now() -> float:
    return time.time()


def _coerce_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


def _backup(path: Path) -> None:
    """把损坏文件挪到 .bak；备份失败只记日志，记忆是尽力而为。"""
    target = path.with_name(path.name + ".bak")
    try:
        if target.exists():
            target = path.with_name(
                f"{path.name}.{int(time.time())}.{uuid.uuid4().hex[:4]}.bak"
            )
        path.replace(target)
    except OSError as e:
        log.warning("记忆文件备份失败：%s (%s)", path, e)


def _atomic_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _load(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("记忆文件损坏，已备份：%s (%s)", path, e)
        _backup(path)
        return default
    if not isinstance(data, dict):
        log.warning("记忆文件结构非法，已备份：%s", path)
        _backup(path)
        return default
    try:
        version = int(data.get("version", 0))
    except (TypeError, ValueError) as e:
        log.warning("记忆文件版本非法，已备份：%s (%s)", path, e)
        _backup(path)
        return default
    if version > VERSION:
        log.warning("记忆文件版本过高，已备份：%s", path)
        _backup(path)
        return default
    data["version"] = VERSION
    return data


def _empty_profile() -> dict:
    return {"version": VERSION, "updated_at": 0.0, "turn_count": 0, "traits": {}}


def _empty_concepts() -> dict:
    return {"version": VERSION, "concepts": []}


def _empty_card() -> dict:
    return {
        "title": "",
        "domains": [],
        "one_liner": "",
        "summary": "",
        "facts": [],
        "summarized_through": "",
        "updated_at": 0.0,
    }


def _empty_chat(paper_id: str) -> dict:
    return {
        "version": VERSION,
        "paper_id": paper_id,
        "messages": [],
        "card": _empty_card(),
    }


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _clean_messages(value: Any) -> list[dict]:
    if not isinstance(value, list):
        return []
    out: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        message_id = item.get("id")
        if not isinstance(message_id, str) or not message_id:
            continue
        if item.get("role") not in ("user", "ai"):
            continue
        msg = dict(item)
        msg["text"] = _coerce_text(msg.get("text"))
        out.append(msg)
    return out


def _clean_domains(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, (dict, list)):
            continue
        text = _coerce_text(item)
        if not text:
            continue
        out.append(text)
        if len(out) >= 4:
            break
    return out


def _clean_facts(value: Any) -> list[dict]:
    if not isinstance(value, list):
        return []
    out: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if not isinstance(kind, str) or kind not in FACT_KINDS:
            continue
        fact = dict(item)
        fact["text"] = _coerce_text(fact.get("text"))
        out.append(fact)
    return out


def _public_message(msg: dict) -> dict:
    out = {
        "id": msg.get("id", ""),
        "role": msg.get("role", ""),
        "text": msg.get("text", ""),
        "at": msg.get("at", 0.0),
    }
    if msg.get("sources"):
        out["sources"] = msg["sources"]
    if msg.get("used_web"):
        out["usedWeb"] = True
    if msg.get("latency") is not None:
        out["latency"] = msg["latency"]
    return out


class MemoryStore:
    def __init__(self, root: Path):
        self.root = Path(root) / "memory"
        self.papers = self.root / "papers"
        self.papers.mkdir(parents=True, exist_ok=True)
        self.profile_path = self.root / "profile.json"
        self.concepts_path = self.root / "concepts.json"
        self._lock = threading.RLock()

    # ── 路径 ────────────────────────────────────────────
    def paper_path(self, paper_id: str) -> Path:
        return self.papers / f"{paper_id}.chat.json"

    # ── 读取 ────────────────────────────────────────────
    def chat(self, paper_id: str) -> dict:
        data = _load(self.paper_path(paper_id), _empty_chat(paper_id))
        data["messages"] = _clean_messages(data.get("messages"))
        card = data.get("card")
        if not isinstance(card, dict):
            card = _empty_card()
        for key, value in _empty_card().items():
            card.setdefault(key, value)
        card["domains"] = _clean_domains(card.get("domains"))
        card["facts"] = _clean_facts(card.get("facts"))
        data["card"] = card
        return data

    def profile(self) -> dict:
        data = _load(self.profile_path, _empty_profile())
        if not isinstance(data.get("traits"), dict):
            data["traits"] = {}
        return data

    def concepts(self) -> dict:
        data = _load(self.concepts_path, _empty_concepts())
        if not isinstance(data.get("concepts"), list):
            data["concepts"] = []
        return data

    # ── 保存 ────────────────────────────────────────────
    def _save_chat(self, paper_id: str, data: dict) -> None:
        _atomic_write(self.paper_path(paper_id), data)

    # ── 论文元信息 ──────────────────────────────────────
    def seed(self, paper_id: str, title: str = "", domains: list[str] | None = None) -> None:
        if not enabled():
            return
        with self._lock:
            chat = self.chat(paper_id)
            card = chat["card"]
            changed = False
            if title and not card.get("title"):
                card["title"] = _clip(title, 200)
                changed = True
            if domains:
                merged = list(dict.fromkeys([*card.get("domains", []), *domains]))[:4]
                if merged != card.get("domains"):
                    card["domains"] = merged
                    changed = True
            if changed:
                self._save_chat(paper_id, chat)

    # ── 消息流 ──────────────────────────────────────────
    def begin_question(
        self, paper_id: str, question: str, client_id: str | None = None
    ) -> tuple[dict, dict | None]:
        if not enabled():
            msg = {
                "id": f"m_{uuid.uuid4().hex[:12]}",
                "role": "user",
                "text": _clip(question, 2000),
                "at": _now(),
            }
            if client_id:
                msg["client_id"] = client_id
            return msg, None

        with self._lock:
            chat = self.chat(paper_id)
            if client_id:
                for m in chat["messages"]:
                    if m.get("role") == "user" and m.get("client_id") == client_id:
                        answer = next(
                            (x for x in chat["messages"] if x.get("answer_to") == m["id"]),
                            None,
                        )
                        return m, answer
            msg = {
                "id": f"m_{uuid.uuid4().hex[:12]}",
                "role": "user",
                "text": _clip(question, 2000),
                "at": _now(),
            }
            if client_id:
                msg["client_id"] = client_id
            chat["messages"].append(msg)
            self._save_chat(paper_id, chat)
            return msg, None

    def finish_answer(
        self,
        paper_id: str,
        user_message_id: str,
        text: str,
        sources: list[dict],
        used_web: bool,
        latency: float,
    ) -> dict:
        msg = {
            "id": f"m_{uuid.uuid4().hex[:12]}",
            "role": "ai",
            "text": text,
            "sources": sources,
            "used_web": bool(used_web),
            "latency": latency,
            "answer_to": user_message_id,
            "at": _now(),
        }
        if not enabled():
            return msg
        with self._lock:
            chat = self.chat(paper_id)
            existing = next(
                (
                    x
                    for x in chat["messages"]
                    if x.get("role") == "ai" and x.get("answer_to") == user_message_id
                ),
                None,
            )
            if existing is not None:
                return existing
            chat["messages"].append(msg)
            self._save_chat(paper_id, chat)
        return msg

    def recent_before(self, paper_id: str, message_id: str, n: int = 6) -> list[dict]:
        if not enabled() or n <= 0:
            return []
        messages = self.chat(paper_id)["messages"]
        index = next(
            (i for i, m in enumerate(messages) if m.get("id") == message_id), None
        )
        if index is None:
            return []
        return messages[:index][-n:]

    def page(self, paper_id: str, before: str | None = None, limit: int = 50) -> dict:
        if not enabled():
            return {"messages": [], "hasMore": False}
        messages = self.chat(paper_id)["messages"]
        end = len(messages)
        if before:
            index = next(
                (i for i, m in enumerate(messages) if m.get("id") == before), None
            )
            if index is None:
                return {"messages": [], "hasMore": False}
            end = index
        start = max(0, end - max(1, min(limit, 200)))
        return {
            "messages": [_public_message(m) for m in messages[start:end]],
            "hasMore": start > 0,
        }

    def forget(self, paper_id: str) -> None:
        with self._lock:
            path = self.paper_path(paper_id)
            targets = [path]
            try:
                entries = list(self.papers.iterdir())
            except OSError:
                entries = []
            for entry in entries:
                name = entry.name
                if name.startswith(path.name + ".") and (
                    name.endswith(".bak") or name.endswith(".tmp")
                ):
                    targets.append(entry)
            for target in targets:
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    pass


_stores: dict[str, MemoryStore] = {}
_stores_lock = threading.Lock()


def get_store(root: Path) -> MemoryStore:
    key = str(Path(root).resolve())
    store = _stores.get(key)
    if store is not None:
        return store
    with _stores_lock:
        store = _stores.get(key)
        if store is None:
            store = MemoryStore(Path(root))
            _stores.setdefault(key, store)
        return store
