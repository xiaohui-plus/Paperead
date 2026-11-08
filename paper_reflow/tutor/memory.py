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

from .qa import BM25, tokenize

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
FACT_PRIORITY = {"conclusion": 0, "open_question": 1, "user_stance": 2}
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
        log.warning("记忆文件损坏，准备备份：%s (%s)", path, e)
        _backup(path)
        return default
    if not isinstance(data, dict):
        log.warning("记忆文件结构非法，准备备份：%s", path)
        _backup(path)
        return default
    try:
        version = int(data.get("version", 0))
    except (TypeError, ValueError, OverflowError) as e:
        log.warning("记忆文件版本非法，准备备份：%s (%s)", path, e)
        _backup(path)
        return default
    if version > VERSION:
        log.warning("记忆文件版本过高，准备备份：%s", path)
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


def _card_text(value: Any, limit: int | None = None) -> str:
    """卡片标量只接受字符串；手改出来的列表/None 一律退化为空串。"""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    return text if limit is None else text[:limit]


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
        if not isinstance(item, str):
            continue
        text = item.strip()[:20]
        if not text or text in out:
            continue
        out.append(text)
        if len(out) >= 4:
            break
    return out


def _clean_facts(value: Any) -> list[dict]:
    if not isinstance(value, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if not isinstance(kind, str) or kind not in FACT_KINDS:
            continue
        text = item.get("text")
        if not isinstance(text, str):
            continue
        text = text.strip()
        if not 2 <= len(text) <= 120 or text in seen:
            continue
        seen.add(text)
        fact = dict(item)
        fact["text"] = text
        out.append(fact)
    if len(out) > MAX_FACTS:
        keep = sorted(
            range(len(out)),
            key=lambda i: (FACT_PRIORITY.get(out[i].get("kind"), 0), i),
        )[-MAX_FACTS:]
        out = [out[i] for i in sorted(keep)]
    return out


def _concept_key(name: str) -> str:
    return re.sub(r"[\s。，、！？!?.,;；:：]+", " ", (name or "").strip().lower()).strip()


def _clean_traits(value: Any) -> dict:
    if not isinstance(value, dict):
        return {}
    out: dict[str, dict] = {}
    for key, raw in value.items():
        if key not in TRAIT_KEYS or not isinstance(raw, dict):
            continue
        text = raw.get("value")
        if not isinstance(text, str):
            continue
        text = text.strip()
        if not 2 <= len(text) <= 40:
            continue
        try:
            confidence = float(raw.get("confidence", 0.5))
        except (TypeError, ValueError, OverflowError):
            confidence = 0.5
        confidence = min(0.9, max(0.3, confidence))
        evidence: list[str] = []
        raw_evidence = raw.get("evidence")
        if isinstance(raw_evidence, list):
            for item in raw_evidence:
                if isinstance(item, str) and item.strip():
                    evidence.append(item.strip()[:60])
        try:
            updated_at = float(raw.get("updated_at", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            updated_at = 0.0
        out[key] = {
            "value": text,
            "confidence": round(confidence, 3),
            "evidence": evidence[-3:],
            "updated_at": updated_at,
        }
    return out


def _clean_concepts(value: Any) -> list[dict]:
    if not isinstance(value, list):
        return []
    out: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        raw_name = item.get("name")
        raw_key = item.get("key")
        name = raw_name.strip() if isinstance(raw_name, str) else ""
        key = raw_key.strip() if isinstance(raw_key, str) else ""
        if not name:
            name = key
        if not name:
            continue
        key = _concept_key(key or name)
        if not key:
            continue
        state = item.get("state")
        if not isinstance(state, str) or state not in STATE_RANK:
            continue
        try:
            confidence = float(item.get("confidence"))
        except (TypeError, ValueError, OverflowError):
            continue
        confidence = min(0.9, max(0.3, confidence))
        aliases: list[str] = []
        raw_aliases = item.get("aliases")
        if isinstance(raw_aliases, list):
            for alias in raw_aliases:
                if isinstance(alias, str) and alias.strip():
                    aliases.append(alias.strip()[:40])
        papers: list[str] = []
        raw_papers = item.get("papers")
        if isinstance(raw_papers, list):
            for paper_id in raw_papers:
                if isinstance(paper_id, str) and paper_id.strip():
                    papers.append(paper_id)
        evidence: list[dict] = []
        raw_evidence = item.get("evidence")
        if isinstance(raw_evidence, list):
            evidence = [dict(entry) for entry in raw_evidence if isinstance(entry, dict)]
        try:
            times_asked = max(1, int(item.get("times_asked", 1)))
        except (TypeError, ValueError, OverflowError):
            times_asked = 1
        try:
            last_seen = float(item.get("last_seen", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            last_seen = 0.0
        out.append(
            {
                "key": key,
                "name": name[:40],
                "aliases": list(dict.fromkeys(aliases))[-5:],
                "state": state,
                "confidence": round(confidence, 3),
                "evidence": evidence[-5:],
                "papers": list(dict.fromkeys(papers))[-20:],
                "times_asked": times_asked,
                "last_seen": last_seen,
            }
        )
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
        card["title"] = _card_text(card.get("title"), 200)
        card["summary"] = _card_text(card.get("summary"), 400)
        card["one_liner"] = _card_text(card.get("one_liner"), 60)
        card["summarized_through"] = _card_text(card.get("summarized_through"))
        try:
            updated_at = float(card.get("updated_at", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            updated_at = 0.0
        card["updated_at"] = updated_at
        data["card"] = card
        return data

    def profile(self) -> dict:
        data = _load(self.profile_path, _empty_profile())
        data["traits"] = _clean_traits(data.get("traits"))
        try:
            turn_count = int(data.get("turn_count", 0))
        except (TypeError, ValueError, OverflowError):
            turn_count = 0
        data["turn_count"] = max(0, turn_count)
        try:
            updated_at = float(data.get("updated_at", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            updated_at = 0.0
        data["updated_at"] = updated_at
        return data

    def concepts(self) -> dict:
        data = _load(self.concepts_path, _empty_concepts())
        data["concepts"] = _clean_concepts(data.get("concepts"))
        return data

    # ── 保存 ────────────────────────────────────────────
    def _save_chat(self, paper_id: str, data: dict) -> None:
        _atomic_write(self.paper_path(paper_id), data)

    # ── 论文元信息 ──────────────────────────────────────
    def seed(self, paper_id: str, title: str = "", domains: list[str] | None = None) -> None:
        if not enabled():
            return
        title = _card_text(title, 200)
        domains = _clean_domains(domains)
        with self._lock:
            chat = self.chat(paper_id)
            card = chat["card"]
            changed = False
            if title and not card.get("title"):
                card["title"] = _clip(title, 200)
                changed = True
            if domains:
                merged = _clean_domains([*card.get("domains", []), *domains])
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

    # ── 合并（画像 / 概念 / 记忆卡）──────────────────
    def apply_patch(self, paper_id: str, patch: dict, touched: list[str] | None = None) -> None:
        if not enabled() or not isinstance(patch, dict):
            return
        now = _now()
        with self._lock:
            profile = self.profile()
            self._merge_traits(profile, patch.get("traits") or [], now)

            concepts = self.concepts()
            self._merge_concepts(concepts, patch.get("concepts") or [], paper_id, now)

            chat = self.chat(paper_id)
            self._merge_card(chat, patch, now)

            profile["updated_at"] = now
            profile["turn_count"] = int(profile.get("turn_count", 0)) + 1
            _atomic_write(self.profile_path, profile)
            _atomic_write(self.concepts_path, concepts)
            self._save_chat(paper_id, chat)

    def _merge_traits(self, profile: dict, items: list, now: float) -> None:
        if not isinstance(items, list):
            return
        traits = profile.setdefault("traits", {})
        for item in (items or [])[:3]:
            if not isinstance(item, dict):
                continue
            key = _patch_text(item.get("key"))
            value = _patch_text(item.get("value"))
            evidence = _patch_text(item.get("evidence"))
            if key not in TRAIT_KEYS or not (2 <= len(value) <= 40) or not evidence:
                continue
            try:
                confidence = float(item.get("confidence", 0.5))
            except (TypeError, ValueError, OverflowError):
                confidence = 0.5
            confidence = min(0.9, max(0.3, confidence))

            old = traits.get(key)
            if old is not None and not isinstance(old, dict):
                continue
            if old:
                base = float(old.get("confidence", 0.5))
                merged = 0.6 * base + 0.4 * confidence
                merged = max(base - 0.2, min(base + 0.2, merged))
                merged = min(0.9, max(0.3, merged))
                evidence_list = list(old.get("evidence") or [])[-2:] + [evidence[:60]]
                old.update(
                    value=value,
                    confidence=round(merged, 3),
                    evidence=evidence_list[-3:],
                    updated_at=now,
                )
            else:
                traits[key] = {
                    "value": value,
                    "confidence": round(confidence, 3),
                    "evidence": [evidence[:60]],
                    "updated_at": now,
                }

    def _merge_concepts(self, bucket: dict, items: list, paper_id: str, now: float) -> None:
        if not isinstance(items, list):
            return
        concepts = bucket.setdefault("concepts", [])
        for item in (items or [])[:4]:
            if not isinstance(item, dict):
                continue
            name = _patch_text(item.get("name"))[:40]
            state = _patch_text(item.get("state"))
            if not name or state not in STATE_RANK:
                continue
            explicit = item.get("explicit") is True
            evidence = _patch_text(item.get("evidence"))[:80]
            raw_aliases = item.get("aliases")
            aliases = [
                a.strip()[:40]
                for a in (raw_aliases if isinstance(raw_aliases, list) else [])
                if isinstance(a, str) and a.strip()
            ]
            key = _concept_key(name)
            if not key:
                continue
            concept = _find_concept(concepts, key, name)

            if concept is None:
                concepts.append(
                    {
                        "key": key,
                        "name": name,
                        "aliases": list(
                            dict.fromkeys(([name] if name != key else []) + aliases)
                        )[:5],
                        "state": state,
                        "confidence": 0.4,
                        "evidence": [{"paper_id": paper_id, "quote": evidence, "at": now}],
                        "papers": [paper_id],
                        "times_asked": 1,
                        "last_seen": now,
                    }
                )
                continue

            old_state = concept.get("state", "learning")
            confidence = float(concept.get("confidence", 0.4))
            if STATE_RANK[state] < STATE_RANK[old_state]:
                if not explicit:
                    continue
                concept["state"] = state
                concept["confidence"] = round(max(0.3, confidence - 0.2), 3)
            elif STATE_RANK[state] > STATE_RANK[old_state]:
                concept["state"] = state
                concept["confidence"] = round(min(0.9, confidence + 0.1), 3)
            else:
                concept["confidence"] = round(min(0.9, confidence + 0.05), 3)

            concept["aliases"] = list(
                dict.fromkeys([*(concept.get("aliases") or []), name, *aliases])
            )[:5]
            evidence_list = list(concept.get("evidence") or [])[-4:]
            if evidence:
                evidence_list.append({"paper_id": paper_id, "quote": evidence, "at": now})
            concept["evidence"] = evidence_list[-5:]
            papers = list(concept.get("papers") or [])
            if paper_id not in papers:
                papers.append(paper_id)
            concept["papers"] = papers[-20:]
            concept["times_asked"] = int(concept.get("times_asked", 1)) + 1
            concept["last_seen"] = now

    def _merge_card(self, chat: dict, patch: dict, now: float) -> None:
        card = chat.setdefault("card", _empty_card())
        summary = _patch_text(patch.get("summary"))
        if 40 <= len(summary) <= 400 and summary != card.get("summary"):
            card["summary"] = summary
            card["summarized_through"] = _patch_text(
                next(
                    (
                        m.get("id")
                        for m in reversed(chat.get("messages") or [])
                        if m.get("role") == "ai"
                    ),
                    "",
                )
            )
            card["updated_at"] = now

        facts = card.setdefault("facts", [])
        fact_items = patch.get("facts")
        for item in (fact_items if isinstance(fact_items, list) else [])[:2]:
            if not isinstance(item, dict):
                continue
            kind = _patch_text(item.get("kind"))
            text = _patch_text(item.get("text"))
            if kind not in FACT_KINDS or not (2 <= len(text) <= 120):
                continue
            if any(_jaccard(text, f.get("text", "")) >= 0.7 for f in facts):
                continue
            facts.append({"kind": kind, "text": text, "at": now})

        while len(facts) > MAX_FACTS:
            index = min(
                range(len(facts)),
                key=lambda i: (FACT_PRIORITY.get(facts[i].get("kind"), 0), i),
            )
            facts.pop(index)

        card_patch = patch.get("card") if isinstance(patch.get("card"), dict) else {}
        one_liner = _patch_text(card_patch.get("one_liner"))
        if one_liner and not card.get("one_liner") and len(one_liner) <= 60:
            card["one_liner"] = one_liner
        domains = _clean_domains(card_patch.get("domains"))
        if domains:
            card["domains"] = _clean_domains([*card.get("domains", []), *domains])

    def fallback_patch(self, question: str, touched: list[str] | None = None) -> dict:
        q = question or ""
        names = [n for n in (touched or []) if n][:1]
        patch: dict = {"concepts": [], "facts": []}

        if re.search(r"不对|错了|不是这个|(?<!没)有误|不是这样", q):
            patch["facts"].append({"kind": "user_stance", "text": f"用户纠正：{_clip(q, 80)}"})

        if re.search(
            r"不懂|没懂|没听懂|没听明白|没看懂|看不懂|不明白|没明白|不清楚|没清楚|"
            r"不理解|没理解|不太懂|听不太懂|没学会|没搞懂|再说一遍|换个说法",
            q,
        ):
            if names:
                patch["concepts"].append(
                    {"name": names[0], "state": "confused", "evidence": "用户表示没懂", "explicit": True}
                )
                patch["facts"].append({"kind": "open_question", "text": f"还没搞懂：{names[0]}"})
            else:
                patch["facts"].append({"kind": "open_question", "text": "有一个问题还没搞懂"})
            return patch

        if re.search(r"(?<![不没])(懂了|明白了|清楚了|可以了|理解了)", q) and names:
            patch["concepts"].append(
                {"name": names[0], "state": "understood", "evidence": "用户表示已懂", "explicit": True}
            )
        return patch

    # ── 注入块 ──────────────────────────────────────────
    def build_brief(
        self, paper_id: str, question: str, exclude_message_id: str | None = None
    ) -> str:
        if not enabled():
            return ""
        now = _now()
        chat = self.chat(paper_id)
        parts: list[str] = []

        profile_lines = self._profile_lines(now)
        if profile_lines:
            parts.append("【关于读者】\n" + "\n".join(profile_lines))

        concept_lines = self._concept_lines(question)
        if concept_lines:
            parts.append("【概念理解】\n" + "\n".join(concept_lines))

        card_block = self._card_block(chat)
        if card_block:
            parts.append(card_block)

        old_lines = self._old_turns(chat, question, exclude_message_id)
        if old_lines:
            parts.append("【此前问答】\n" + "\n".join(old_lines))

        cross_lines = self._cross_paper(paper_id, question)
        if cross_lines:
            parts.append("【读过的相关论文】\n" + "\n".join(cross_lines))

        repeat = self._repeat_block(chat, question, exclude_message_id)
        if repeat:
            parts.append(repeat)
        return "\n\n".join(parts)

    def _profile_lines(self, now: float) -> list[str]:
        profile = self.profile()
        lines = []
        for key in TRAIT_KEYS:
            trait = (profile.get("traits") or {}).get(key)
            if not trait:
                continue
            days = max(0.0, (now - float(trait.get("updated_at", 0))) / 86400)
            effective = float(trait.get("confidence", 0)) * (0.5 ** (days / 180))
            if effective >= 0.55:
                lines.append(f"- {TRAIT_LABELS[key]}：{trait.get('value', '')}")
        return lines

    def _concept_lines(self, question: str) -> list[str]:
        items = self.match_concepts(question)[:4]
        return [f"- {c['name']}：{STATE_LABELS.get(c.get('state', 'learning'), '在学')}" for c in items]

    def _card_block(self, chat: dict) -> str:
        card = chat.get("card") or {}
        lines = []
        if card.get("summary"):
            lines.append(card["summary"])
        for fact in (card.get("facts") or [])[-2:]:
            lines.append(f"- {FACT_LABELS.get(fact.get('kind'), '记录')}：{fact.get('text', '')}")
        return "【这篇聊到哪】\n" + "\n".join(lines) if lines else ""

    def _old_turns(self, chat: dict, question: str, exclude_id: str | None) -> list[str]:
        messages = chat.get("messages") or []
        recent_ids = {m["id"] for m in messages[-6:]}
        if exclude_id:
            recent_ids.add(exclude_id)
        candidates = []
        answers = {m.get("answer_to"): m for m in messages if m.get("role") == "ai"}
        anchor = next(
            (i for i, m in enumerate(messages) if m["id"] == exclude_id), len(messages)
        )
        window = messages[max(0, anchor - INDEX_WINDOW):anchor]
        for msg in window[:-6]:
            if msg.get("role") != "user" or msg["id"] in recent_ids:
                continue
            answer = answers.get(msg["id"])
            text = msg.get("text", "")
            if answer:
                text = f"{text}\n{answer.get('text', '')}"
            candidates.append((msg, text))
        if not candidates:
            return []
        bm = BM25([tokenize(text) for _, text in candidates])
        scores = bm.score(tokenize(question))
        ranked = sorted(zip(candidates, scores), key=lambda item: item[1], reverse=True)
        lines = []
        for (msg, text), score in ranked[:3]:
            if score <= 0:
                continue
            answer = answers.get(msg["id"])
            lines.append(
                f"问：{_clip(msg.get('text', ''), 120)}\n"
                f"答：{_clip(answer.get('text', '') if answer else '', 180)}"
            )
        return lines

    def _cross_paper(self, paper_id: str, question: str) -> list[str]:
        docs = []
        for path in self.papers.glob("*.chat.json"):
            other_id = path.name[: -len(".chat.json")]
            if not other_id or other_id == paper_id:
                continue
            # 走 chat() 归一化：手改过的标量/域名不能让 " ".join 崩溃
            card = self.chat(other_id)["card"]
            if not card.get("summary") and not card.get("one_liner"):
                continue
            facts = "；".join(f.get("text", "") for f in (card.get("facts") or [])[-2:])
            text = " ".join(
                [
                    card.get("title", ""),
                    " ".join(card.get("domains") or []),
                    card.get("one_liner", ""),
                    card.get("summary", ""),
                    facts,
                ]
            )
            docs.append((card, text))
        if not docs:
            return []
        bm = BM25([tokenize(text) for _, text in docs])
        scores = bm.score(tokenize(question))
        ranked = sorted(zip(docs, scores), key=lambda item: item[1], reverse=True)
        lines = []
        for (card, _), score in ranked[:2]:
            if score < 0.3:
                continue
            stance = next(
                (f.get("text", "") for f in reversed(card.get("facts") or []) if f.get("kind") == "user_stance"),
                "",
            )
            line = f"《{_clip(card.get('title', '未命名'), 60)}》：{_clip(card.get('one_liner') or card.get('summary', ''), 90)}"
            if stance:
                line += f"；你当时的判断：{_clip(stance, 60)}"
            lines.append(line)
        return lines

    def _repeat_block(self, chat: dict, question: str, exclude_id: str | None) -> str:
        messages = chat.get("messages") or []
        answers = {m.get("answer_to"): m for m in messages if m.get("role") == "ai"}
        for msg in reversed(messages[:-1]):
            if msg.get("role") != "user" or msg.get("id") == exclude_id:
                continue
            if _jaccard(question, msg.get("text", "")) >= 0.6:
                answer = answers.get(msg["id"])
                return (
                    "【此前问过】\n"
                    f"问：{_clip(msg.get('text', ''), 120)}\n"
                    f"答：{_clip(answer.get('text', '') if answer else '', 200)}\n"
                    "（若本轮没有新增信息，简要复述结论并说明是否有变化，不要装作第一次被问）"
                )
        return ""

    def match_concepts(self, question: str) -> list[dict]:
        q = (question or "").lower()
        q_tokens = set(tokenize(question))
        scored = []
        for concept in self.concepts().get("concepts") or []:
            names = [concept.get("name", "")] + list(concept.get("aliases") or [])
            hits = 0
            for name in names:
                if name and name.lower() in q:
                    hits += 2
                elif set(tokenize(name)) & q_tokens:
                    hits += 1
            if hits:
                scored.append((hits, concept))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [c for _, c in scored]

    def touched_concepts(self, question: str) -> list[str]:
        if not enabled():
            return []
        return [c.get("name", "") for c in self.match_concepts(question)[:2]]

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


def _patch_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _norm_alias(text: str) -> str:
    return _concept_key(text)


def _find_concept(concepts: list, key: str, name: str) -> dict | None:
    if not key:
        return None
    wanted = {key, _norm_alias(name)}
    for concept in concepts:
        if not isinstance(concept, dict):
            continue
        candidates = {_norm_alias(concept.get("key", "")), _norm_alias(concept.get("name", ""))}
        candidates |= {_norm_alias(a) for a in concept.get("aliases") or []}
        if wanted & candidates:
            return concept
        for candidate in candidates:
            if len(candidate) >= 3 and (candidate in key or key in candidate):
                return concept
    return None


def _jaccard(a: str, b: str) -> float:
    from .qa import tokenize

    sa, sb = set(tokenize(a)), set(tokenize(b))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


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
