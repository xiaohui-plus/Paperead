"""导读卡生成：概念 + 前置候选 → 结构化导读。

LLM 只负责"选哪个候选、为什么"，不允许自己造文献；返回里出现候选之外的编号一律丢弃。
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter

from ..layout import Block
from ..llm import chat
from .concepts import Concept, extract
from .prereq import Candidate, match
from .references import parse as parse_references

log = logging.getLogger(__name__)

SYS = """你是论文导读助手。基于给定的论文信息，生成一份 3 分钟能读完的导读。

硬约束：
1. summary ≤ 60 字，必须包含"解决什么问题 + 用了什么方法"；
2. prereqs 只能从给定的【概念】里选（最多 5 个），候选论文只能从该概念下的【候选】里选；
3. 每条 prereq 的 why ≤ 40 字，说明"读完它才能看懂本文的哪一部分"；
4. path 从候选里挑 0–3 条作为阅读顺序；
5. 不要评价论文好坏，不要编造数字，不要输出候选之外的文献；
6. 只输出 JSON，不要解释、不要 Markdown 代码块。

输出格式：
{
  "summary": "...",
  "domains": ["...", "..."],
  "difficulty": {"stars": 1-5, "minutes": 20-120, "math": "low|medium|high", "terms": "low|medium|high"},
  "prereqs": [{"concept": "概念名", "candidate": "C1", "why": "..."}],
  "path": [{"candidate": "C1", "minutes": 15, "gain": "读完能获得什么"}]
}"""


def _paper_year(blocks: list[Block]) -> int | None:
    head = " ".join(b.text for b in blocks[:40] if b.text)
    years = [int(y) for y in re.findall(r"\b(20[0-4]\d)\b", head)]
    return Counter(years).most_common(1)[0][0] if years else None


def _abstract(blocks: list[Block]) -> str:
    for i, b in enumerate(blocks):
        if b.kind == "heading" and re.match(r"abstract|摘\s*要", b.text, re.I):
            nxt = next((x for x in blocks[i + 1:] if x.kind == "para"), None)
            return (nxt.text if nxt else "")[:1200]
    for b in blocks:
        if b.kind == "para" and len(b.text) > 200:
            return b.text[:1200]
    return ""


def _headings(blocks: list[Block]) -> list[str]:
    return [b.text for b in blocks if b.kind == "heading"][:16]


def _build_prompt(title: str, abstract: str, headings: list[str],
                  concepts: list[Concept], candidates: list[Candidate]) -> str:
    by_concept: dict[str, list[Candidate]] = {}
    for c in candidates:
        by_concept.setdefault(c.concept.name, []).append(c)

    idx = 0
    lines: list[str] = []
    for concept in concepts:
        group = by_concept.get(concept.name) or []
        if not group:
            continue
        lines.append(f"【概念】{concept.name}（{concept.en}，{concept.kind}）")
        if concept.sections:
            lines.append(f"  出现章节：{'、'.join(concept.sections)}")
        for cand in group:
            idx += 1
            cand.uid = f"C{idx}"
            r = cand.ref
            lines.append(f"  C{idx}. {r.title}（{r.year or '年份未知'}）")
        lines.append("")

    return "\n".join(
        [
            f"【标题】{title}",
            f"【摘要】{abstract}",
            f"【章节】{' / '.join(headings)}",
            "",
            "【概念与候选文献（均来自本文参考文献）】",
            *lines,
        ]
    )


def _strip_json(text: str) -> dict:
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(t)
    except Exception:
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return {}


def _fallback(title: str, concepts: list[Concept], candidates: list[Candidate],
              year: int | None) -> dict:
    seen: set[str] = set()
    prereqs = []
    for c in candidates:
        if c.concept.name in seen:
            continue
        seen.add(c.concept.name)
        prereqs.append(
            {
                "concept": c.concept.name,
                "en": c.concept.en,
                "why": f"本文在「{(c.concept.sections or ['正文'])[0]}」中直接用到",
                "paper": c.ref.to_dict(),
            }
        )
        if len(prereqs) >= 5:
            break
    return {
        "status": "ready",
        "summary": f"本文围绕「{concepts[0].name if concepts else title}」展开。",
        "domains": [],
        "difficulty": {"stars": 3, "minutes": 30, "math": "medium", "terms": "medium"},
        "prereqs": prereqs,
        "path": [{"kind": "paper", "title": "本文", "minutes": 30, "gain": ""}],
        "year": year,
        "concepts": [
            {"name": c.name, "en": c.en, "kind": c.kind, "sections": c.sections}
            for c in concepts
        ],
        "degraded": True,
    }


async def build_guide(blocks: list[Block], glossary: dict[str, str] | None = None) -> dict:
    title = next((b.text for b in blocks if b.kind == "title"), "") or "未命名论文"
    abstract = _abstract(blocks)
    headings = _headings(blocks)
    year = _paper_year(blocks)

    concepts = extract(blocks, glossary, limit=8)
    if not concepts:
        return {**_fallback(title, [], [], year), "summary": "没有抽取到可用的概念。"}

    references = parse_references(blocks)
    candidates = match(concepts, references, paper_year=year)
    if not candidates:
        return _fallback(title, concepts, [], year)

    prompt = _build_prompt(title, abstract, headings, concepts, candidates)

    data: dict = {}
    # 推理模型的 reasoning token 也计入 max_tokens，给小了会返回空
    for budget in (3000, 6000):
        try:
            raw = await chat(
                [{"role": "system", "content": SYS}, {"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=budget,
            )
            data = _strip_json(raw)
            if data.get("summary"):
                break
        except Exception as e:
            log.warning("导读生成失败（max_tokens=%d）：%s", budget, e)

    if not data.get("summary"):
        return _fallback(title, concepts, candidates, year)

    uid2cand = {c.uid: c for c in candidates if c.uid}

    prereqs = []
    for p in (data.get("prereqs") or [])[:5]:
        cand = uid2cand.get(str(p.get("candidate", "")).strip())
        if not cand:
            continue  # 模型编造的编号，丢弃
        prereqs.append(
            {
                "concept": cand.concept.name,
                "en": cand.concept.en,
                "why": (p.get("why") or "")[:80],
                "paper": cand.ref.to_dict(),
            }
        )

    path = [
        {
            "kind": "paper",
            "title": "本文",
            "minutes": int((data.get("difficulty") or {}).get("minutes") or 30),
            "gain": "",
        }
    ]
    picked = []
    for step in (data.get("path") or [])[:3]:
        cand = uid2cand.get(str(step.get("candidate", "")).strip())
        if not cand:
            continue
        picked.append(
            {
                "kind": "ref",
                "title": cand.ref.title,
                "year": cand.ref.year,
                "minutes": int(step.get("minutes") or 20),
                "gain": (step.get("gain") or "")[:60],
            }
        )
    if picked:
        path = picked + path

    return {
        "status": "ready",
        "summary": (data.get("summary") or "")[:120],
        "domains": [d for d in (data.get("domains") or []) if isinstance(d, str)][:4],
        "difficulty": data.get("difficulty") or {},
        "prereqs": prereqs,
        "path": path,
        "year": year,
        "concepts": [
            {"name": c.name, "en": c.en, "kind": c.kind, "sections": c.sections}
            for c in concepts
        ],
    }
