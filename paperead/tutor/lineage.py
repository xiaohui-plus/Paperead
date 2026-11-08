"""思路来源图：本文的每个关键想法，从哪篇来、改了什么。

与"引文网络"的区别：只保留**作者自己在正文里描述过关系**的文献。
每条边都绑定一句原文证据，所以图上每一根线都能点开看到依据。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from ..layout import Block
from ..llm import chat
from .citations import find as find_citations
from .references import Reference, parse as parse_references

log = logging.getLogger(__name__)

ROLES = ("foundation", "inspiration", "contrast", "tool")
ROLE_LABEL = {
    "foundation": "基础",
    "inspiration": "直接启发",
    "contrast": "对比/不足",
    "tool": "沿用",
}

# 章节权重：动机与方法部分的引用才说明"思路来源"
_SECTION_WEIGHT = {"introduction": 1.0, "related": 0.9, "method": 1.0,
                   "approach": 1.0, "background": 0.9, "experiment": 0.6}

_CONTRAST_HINT = re.compile(r"however|unlike|in contrast|limited|fail|ignor|lack|suffer", re.I)
_INSPIRE_HINT = re.compile(r"based on|follow|inspired|extend|motivat|borrow|adopt|build", re.I)
_FOUNDATION_HINT = re.compile(r"survey|review|overview|tutorial|benchmark|first", re.I)
_TOOL_HINT = re.compile(r"dataset|benchmark|metric|protocol|toolkit|implementation", re.I)

# 版权/许可声明常被切进正文，里面也有 [n] 但没有任何学术关系
_NOISE_RE = re.compile(
    r"open access|creative commons|all rights reserved|downloaded from|licen[cs]e",
    re.I,
)

SYS = """你是论文分析助手。给定本文标题，以及本文引用某篇文献时的原句，判断该文献对本文的作用。

role 只能是四者之一：
- foundation：领域基础/综述，提供背景框架
- inspiration：本文直接借用/沿用了它的思路或模块
- contrast：本文指出它的问题或不足，从而引出自己的做法
- tool：数据集、评测协议、工具实现
- unclear：原句只是罗列引用，看不出关系

borrow：≤30 字，本文从它那里获得了什么；role 为 unclear 时留空
improve：≤40 字，本文在它之上改了什么；若不是 contrast/inspiration 可留空

只输出 JSON 数组，不要解释：
[{"id": 3, "role": "inspiration", "borrow": "...", "improve": "..."}]"""


@dataclass
class Node:
    ref_index: int
    title: str
    year: int | None
    role: str = "foundation"
    borrow: str = ""
    improve: str = ""
    evidence: str = ""
    cites: int = 1
    weight: float = 0.0
    marker: str = ""


@dataclass
class Lineage:
    nodes: list[Node] = field(default_factory=list)
    self_title: str = ""
    self_year: int | None = None

    def to_dict(self) -> dict:
        nodes = sorted(self.nodes, key=lambda n: (n.year or 0))
        return {
            "self": {"title": self.self_title, "year": self.self_year},
            "nodes": [
                {
                    "id": n.ref_index,
                    "title": n.title,
                    "year": n.year,
                    "role": n.role,
                    "roleLabel": ROLE_LABEL.get(n.role, n.role),
                    "borrow": n.borrow,
                    "improve": n.improve,
                    "evidence": n.evidence,
                    "cites": n.cites,
                    "marker": n.marker,
                }
                for n in nodes
            ],
            "edges": [
                {"from": n.ref_index, "to": "self", "relation": n.role, "weight": n.cites}
                for n in nodes
            ],
            "yearRange": [
                min((n.year for n in nodes if n.year), default=2000),
                max((n.year for n in nodes if n.year), default=2024),
            ],
        }


def _sentences(text: str) -> list[tuple[int, int, str]]:
    out: list[tuple[int, int, str]] = []
    start = 0
    for m in re.finditer(r"(?<=[.!?])\s+", text):
        out.append((start, m.start(), text[start:m.start()]))
        start = m.end()
    out.append((start, len(text), text[start:]))
    return out


def _evidence_for(text: str, pos: int, limit: int = 240) -> str:
    sents = _sentences(text)
    for i, (s, e, _) in enumerate(sents):
        if s <= pos < e:
            start = s
            # 带上一句上下文，帮助模型判断关系
            if i > 0:
                start = sents[i - 1][0]
            return text[start:e][-limit:].strip()
    return text[:limit]


def _sentence_at(text: str, pos: int) -> tuple[int, int, str]:
    sents = _sentences(text)
    for s, e, seg in sents:
        if s <= pos < e:
            return s, e, seg
    return 0, len(text), text


_DESCRIPTIVE = re.compile(
    r"\b(propose[sd]?|adopt|use[sd]?|utiliz|employ|introduc|calculat|encod|learn|"
    r"show|demonstrat|improve|extend|based on|however|unlike|limit)", re.I
)


def _evidence_score(seg: str) -> float:
    """句子越"在描述关系"越好，只罗列引用编号的句子要扣分。"""
    markers = len(re.findall(r"\[\d", seg))
    verbs = len(_DESCRIPTIVE.findall(seg))
    return verbs * 1.0 - max(0, markers - 1) * 1.2


def _best_evidence(text: str, pos: int, limit: int = 240) -> tuple[str, float]:
    s, e, seg = _sentence_at(text, pos)
    return seg.strip()[:limit], _evidence_score(seg)


def _section_weight(section: str) -> float:
    s = (section or "").lower()
    for key, w in _SECTION_WEIGHT.items():
        if key in s:
            return w
    return 0.8


_STOP = {
    "with", "from", "that", "this", "these", "those", "using", "based", "into",
    "gene", "network", "networks", "method", "methods", "model", "models", "data",
    "inference", "analysis", "study", "paper", "approach", "framework",
}


def _keywords(text: str) -> set[str]:
    return {
        w.lower()
        for w in re.findall(r"[A-Za-z][A-Za-z\-]{3,}", text)
        if w.lower() not in _STOP
    }


def collect(
    blocks: list[Block],
    references: list[Reference],
    limit: int = 10,
    topic: str = "",
) -> list[Node]:
    """统计引用、收集证据、按权重选点。

    权重 = 章节权重之和 + 与本文主题词的标题重合度。
    后者很关键：真正"启发"本文的那篇往往只被引用一次，
    但标题里就带着本文的核心词（如 adversarial / embedding）。
    """
    by_index = {r.index: r for r in references}
    topic_kw = _keywords(topic)
    stats: dict[int, Node] = {}
    best: dict[int, float] = {}

    for b in blocks:
        if b.kind != "para" or not b.text:
            continue
        if _NOISE_RE.search(b.text):
            continue
        for cite in find_citations(b.text):
            if cite.index is None:
                continue
            ref = by_index.get(cite.index)
            if not ref:
                continue
            seg, quality = _best_evidence(b.text, cite.start)
            node = stats.get(cite.index)
            if node is None:
                node = Node(
                    ref_index=ref.index,
                    title=ref.title,
                    year=ref.year,
                    evidence=seg,
                    marker=cite.marker,
                )
                stats[cite.index] = node
                best[cite.index] = quality
            elif quality > best[cite.index]:
                node.evidence = seg
                best[cite.index] = quality
                node.marker = cite.marker
            node.cites += 1
            node.weight += _section_weight(b.section)

    for n in stats.values():
        if topic_kw:
            title_kw = _keywords(n.title)
            overlap = len(title_kw & topic_kw) / max(len(topic_kw), 1)
            n.weight += 1.6 * overlap

    # 证据太弱（纯罗列）的不进图：宁可少画几条，也不画错
    ranked = [n for n in stats.values() if best[n.ref_index] >= 0]
    ranked.sort(key=lambda n: (n.weight, n.cites), reverse=True)
    ranked = ranked[:limit]
    ranked.sort(key=lambda n: (n.year or 0))
    return ranked


def _guess_role(evidence: str, title: str) -> str:
    text = f"{title} {evidence}"
    if _FOUNDATION_HINT.search(title):
        return "foundation"
    if _CONTRAST_HINT.search(evidence):
        return "contrast"
    if _INSPIRE_HINT.search(evidence):
        return "inspiration"
    if _TOOL_HINT.search(evidence):
        return "tool"
    return "foundation"


async def _label(nodes: list[Node], paper_title: str) -> list[Node]:
    """一次批量调用，让模型给每篇文献定角色 + 写"借了什么/改了什么"。

    模型说 unclear（原句只是罗列）的就丢弃——这是图里"宁缺毋滥"的关键。
    """
    lines = []
    for i, n in enumerate(nodes, 1):
        lines.append(f'#{i} 标题：{n.title}（{n.year or "年份未知"}）')
        lines.append(f'   原句：{n.evidence[:200]}')
    prompt = f"本文标题：{paper_title}\n\n以下是本文引用若干文献时的原句，请逐条判断：\n\n" + "\n".join(lines)

    data = []
    for budget in (4000, 8000):
        try:
            raw = await chat(
                [{"role": "system", "content": SYS}, {"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=budget,
            )
            data = _parse_array(raw)
            if data:
                break
        except Exception as e:
            log.warning("脉络标注失败（max_tokens=%d）：%s", budget, e)

    by_id = {i: n for i, n in enumerate(nodes, 1)}
    seen: set[int] = set()
    for item in data:
        try:
            idx = int(item.get("id") or 0)
        except Exception:
            continue
        node = by_id.get(idx)
        if not node:
            continue
        seen.add(idx)
        role = str(item.get("role") or "").strip()
        node.role = role if role in ROLES + ("unclear",) else _guess_role(node.evidence, node.title)
        node.borrow = (item.get("borrow") or "").strip()[:60]
        node.improve = (item.get("improve") or "").strip()[:80]

    llm_ok = bool(data)
    kept: list[Node] = []
    for i, n in enumerate(nodes, 1):
        if not llm_ok:
            # 模型整体不可用：走规则兜底，至少把图跑出来
            if n.role not in ROLES:
                n.role = _guess_role(n.evidence, n.title)
            if not n.borrow:
                n.borrow = n.evidence[:40] + "…"
            kept.append(n)
            continue
        # 模型说过但没给理由 / 判为 unclear → 这条关系不成立，丢弃
        if i not in seen or n.role == "unclear" or not n.borrow:
            continue
        kept.append(n)

    log.info("脉络：%d 个候选 → %d 个有明确关系", len(nodes), len(kept))
    return kept


def _parse_array(text: str) -> list[dict]:
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        data = json.loads(t)
        return data if isinstance(data, list) else []
    except Exception:
        m = re.search(r"\[.*\]", t, re.S)
        if m:
            try:
                data = json.loads(m.group(0))
                return data if isinstance(data, list) else []
            except Exception:
                pass
    return []


async def build_lineage(blocks: list[Block]) -> dict:
    title = next((b.text for b in blocks if b.kind == "title"), "") or "未命名论文"
    years = [int(y) for y in re.findall(r"\b(20[0-4]\d)\b", " ".join(b.text for b in blocks[:40] if b.text))]
    self_year = max(years) if years else None

    references = parse_references(blocks)
    abstract = next(
        (b.text for b in blocks if b.kind == "para" and len(b.text) > 200), ""
    )[:800]
    nodes = collect(blocks, references, topic=f"{title} {abstract}")
    if not nodes:
        return Lineage(self_title=title, self_year=self_year).to_dict()

    nodes = await _label(nodes, title)
    # 同一类"借了什么"只留一篇（比如 5 个特征选择方法都写"背景：特征选择类方法"）
    seen: set[str] = set()
    deduped: list[Node] = []
    for n in sorted(nodes, key=lambda x: (x.cites, x.weight), reverse=True):
        key = re.sub(r"\s+", "", n.borrow)[:10]
        if key and key in seen:
            continue
        seen.add(key)
        deduped.append(n)
    deduped.sort(key=lambda n: (n.year or 0))
    return Lineage(nodes=deduped, self_title=title, self_year=self_year).to_dict()
