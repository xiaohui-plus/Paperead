"""思路来源图：本文的每个关键想法，从哪篇来、改了什么。

作者几乎不会在正文里直说"我们借鉴了 X"。所以这里不只看引用原句，
而是综合：本文标题与摘要、被引文献标题与年份、引用所在章节、
上下几句的论述，以及文献本身在领域中的位置，推断它对本文的作用。

为了不被错误数据带偏，前端对推断出来的关系会标注置信度；
证据只能引用原文，模型编造的句子会被丢弃。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..layout import Block
from ..llm import chat
from .citations import (
    Citation,
    find as find_citations,
    find_paren_numeric,
    match_index,
)
from .references import Reference, parse as parse_references

log = logging.getLogger(__name__)

# 输出结构变更时递增，旧缓存会被自动作废重算
LINEAGE_VERSION = 2

ROLES = ("foundation", "inspiration", "contrast", "tool")
ROLE_LABEL = {
    "foundation": "基础",
    "inspiration": "直接启发",
    "contrast": "对比/不足",
    "tool": "沿用",
}

# 章节权重：动机与方法部分的引用才说明"思路来源"
_SECTION_WEIGHT = {
    "introduction": 1.0,
    "related": 0.9,
    "method": 1.0,
    "approach": 1.0,
    "background": 0.9,
    "experiment": 0.55,
    "result": 0.5,
    "discussion": 0.6,
    "conclusion": 0.35,
}

_CONTRAST_HINT = re.compile(r"however|unlike|in contrast|limited|fail|ignor|lack|suffer", re.I)
_INSPIRE_HINT = re.compile(r"based on|follow|inspired|extend|motivat|borrow|adopt|build", re.I)
_FOUNDATION_HINT = re.compile(r"survey|review|overview|tutorial|benchmark|first", re.I)
_TOOL_HINT = re.compile(r"dataset|benchmark|metric|protocol|toolkit|implementation", re.I)

# 版权/许可声明常被切进正文，里面也有 [n] 但没有任何学术关系
_NOISE_RE = re.compile(
    r"open access|creative commons|all rights reserved|downloaded from|licen[cs]e",
    re.I,
)
_UNRELATED_RE = re.compile(r"acknowledg|funding|conflict of interest|author contribution", re.I)

SYS = """你是论文分析助手。你的任务不是复述引用句，而是推断被引文献对本文的实质作用。
作者很少直接写"我们借鉴了 X"，必须结合本文主题、被引文献标题、引用所在章节和上下文来推断。

role 只能是：
- foundation：领域基础/综述/背景框架，本文建立在它描述的问题或框架上
- inspiration：本文沿用、改造或扩展了它的思路、模块、架构
- contrast：本文指出它的问题/不足，并据此提出自己的做法
- tool：数据集、评测协议、指标、工具或实现细节
- unclear：文献与本文主题明显无关，或信息不足以做任何合理推断

判断规则：
1. 即使引用句只是编号罗列，也要根据被引文献标题、引用章节和本文主题推断；
   领域经典工作、本文方法直接依赖的基础工作，应当给出 role 和 borrow。
2. borrow 要具体说明"借了什么"（方法/架构/问题定义/数据协议），不要写"提供了背景"这类空话。
3. improve 说明本文相对它改了什么；不是 inspiration/contrast 时可以留空。
4. confidence：high（原文明确描述或标题高度吻合）、medium（可合理推断）、low（仅凭常识猜测）。
5. evidence 只能从给出的原文中摘录一句；没有就直接留空，禁止编造。

只输出 JSON 数组，不要解释：
[{"id": 3, "role": "inspiration", "borrow": "...", "improve": "...", "confidence": "high", "evidence": "..."}]"""


@dataclass
class Node:
    ref_index: int
    title: str
    year: int | None
    role: str = "foundation"
    borrow: str = ""
    improve: str = ""
    evidence: str = ""
    context: str = ""
    cites: int = 1
    weight: float = 0.0
    marker: str = ""
    confidence: str = "medium"
    sections: set[str] = field(default_factory=set)


@dataclass
class Lineage:
    nodes: list[Node] = field(default_factory=list)
    self_title: str = ""
    self_year: int | None = None
    stats: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        nodes = sorted(self.nodes, key=lambda n: (n.year or 0))
        return {
            "version": LINEAGE_VERSION,
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
                    "confidence": n.confidence,
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
            "stats": self.stats,
        }


def _sentences(text: str) -> list[tuple[int, int, str]]:
    out: list[tuple[int, int, str]] = []
    start = 0
    for m in re.finditer(r"(?<=[.!?])\s+", text):
        out.append((start, m.start(), text[start:m.start()]))
        start = m.end()
    out.append((start, len(text), text[start:]))
    return out


def _sentence_at(text: str, pos: int) -> tuple[int, int, str]:
    for s, e, seg in _sentences(text):
        if s <= pos < e:
            return s, e, seg
    return 0, len(text), text


def _best_evidence(text: str, pos: int, limit: int = 240) -> tuple[str, float]:
    s, e, seg = _sentence_at(text, pos)
    return seg.strip()[:limit], _evidence_score(seg)


def _context_window(text: str, pos: int, limit: int = 480) -> str:
    """引用处前后各一句，给模型判断关系用。"""
    sents = _sentences(text)
    idx = 0
    for i, (s, e, _) in enumerate(sents):
        if s <= pos < e:
            idx = i
            break
    start = sents[max(0, idx - 1)][0]
    end = sents[min(len(sents) - 1, idx + 1)][1]
    return text[start:end].strip()[:limit]


_DESCRIPTIVE = re.compile(
    r"\b(propose[sd]?|adopt|use[sd]?|utiliz|employ|introduc|calculat|encod|learn|"
    r"show|demonstrat|improve|extend|based on|however|unlike|limit)",
    re.I,
)


def _evidence_score(seg: str) -> float:
    """句子越"在描述关系"越好，只罗列引用编号的句子要扣分。"""
    markers = len(re.findall(r"\[\d", seg))
    verbs = len(_DESCRIPTIVE.findall(seg))
    return verbs * 1.0 - max(0, markers - 1) * 1.2


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


def _marker_pos(text: str, indices: set[int]) -> int:
    """上标数字被压平进正文时，找一个"字母后紧跟该编号"的位置。"""
    for m in re.finditer(r"\d{1,3}", text):
        if int(m.group(0)) not in indices:
            continue
        if m.start() > 0 and text[m.start() - 1].isalpha():
            return m.start()
    return 0


def _superscript_hits(
    pdf_path: Path, blocks: list[Block], refs: dict[int, Reference]
) -> dict[str, set[int]]:
    """从 PDF 字体信息里提取真正的上标数字引用。

    Nature/Science 的数字引用在正文抽取时会变成 `paradigm16–18`，
    单靠文本无法和 `HEK3`、`Fig3` 这类名字区分；字体大小和 flags 才可靠。
    """
    try:
        import pymupdf
    except Exception:  # pragma: no cover
        return {}

    page_blocks: dict[int, list[Block]] = {}
    for b in blocks:
        if b.kind == "para" and b.page:
            page_blocks.setdefault(b.page, []).append(b)
    if not page_blocks:
        return {}

    hits: dict[str, set[int]] = {}
    try:
        doc = pymupdf.open(str(pdf_path))
    except Exception:
        return {}
    try:
        for pno, page in enumerate(doc, 1):
            candidates = page_blocks.get(pno)
            if not candidates:
                continue
            data = page.get_text("dict")
            sizes: dict[int, int] = {}
            for blk in data.get("blocks", []):
                for line in blk.get("lines", []):
                    for sp in line.get("spans", []):
                        txt = sp.get("text", "").strip()
                        if txt:
                            key = round(sp.get("size", 0))
                            sizes[key] = sizes.get(key, 0) + len(txt)
            body = max(sizes, key=sizes.get) if sizes else 0

            for blk in data.get("blocks", []):
                for line in blk.get("lines", []):
                    spans = line.get("spans", [])
                    for i, sp in enumerate(spans):
                        txt = sp.get("text", "").strip()
                        if not re.fullmatch(r"\d{1,3}(?:\s*[,\-–]\s*\d{1,3})*", txt):
                            continue
                        is_sup = bool(sp.get("flags", 0) & 1) or (
                            body and sp.get("size", 0) <= body * 0.78
                        )
                        if not is_sup:
                            continue
                        prev = ""
                        for j in range(i - 1, -1, -1):
                            if spans[j].get("text", "").strip():
                                prev = spans[j]["text"].strip()
                                break
                        if not re.search(r"[A-Za-z]{2,}[^A-Za-z]{0,3}$", prev):
                            continue
                        idxs = {
                            int(x)
                            for x in re.findall(r"\d{1,3}", txt)
                            if int(x) in refs
                        }
                        if not idxs:
                            continue
                        block = _block_at(candidates, sp.get("bbox"))
                        if block is None:
                            continue
                        first = re.split(r"\s*[,\-–]\s*", txt)[0]
                        if not re.search(rf"(?<=[A-Za-z]){re.escape(first)}", block.text):
                            continue
                        hits.setdefault(block.id, set()).update(idxs)
    finally:
        doc.close()
    return hits


def _block_at(blocks: list[Block], bbox) -> Block | None:
    if not bbox:
        return None
    cx = (bbox[0] + bbox[2]) / 2
    cy = (bbox[1] + bbox[3]) / 2
    for b in blocks:
        x0, y0, x1, y1 = b.bbox
        if x0 - 2 <= cx <= x1 + 2 and y0 - 2 <= cy <= y1 + 2:
            return b
    return None


def _count_unique_citations(blocks: list[Block], references: list[Reference]) -> int:
    seen: set[int] = set()
    for b in blocks:
        if b.kind != "para" or not b.text:
            continue
        for c in find_citations(b.text):
            idx = match_index(c, references)
            if idx is not None:
                seen.add(idx)
    return len(seen)


def _count_paren_citations(blocks: list[Block], references: list[Reference]) -> int:
    seen: set[int] = set()
    for b in blocks:
        if b.kind != "para" or not b.text:
            continue
        for c in find_paren_numeric(b.text):
            idx = match_index(c, references)
            if idx is not None:
                seen.add(idx)
    return len(seen)


def collect(
    blocks: list[Block],
    references: list[Reference],
    limit: int = 12,
    topic: str = "",
    superscript: dict[str, set[int]] | None = None,
    paren_numeric: bool = False,
) -> list[Node]:
    """统计引用、收集证据与上下文、按权重选点。

    权重 = 章节权重之和 + 与本文主题词的标题重合度 + 被引次数。
    真正"启发"本文的那篇往往只被引用一次，但标题里带着本文的核心词，
    所以标题重合度必须参与排序。
    """
    by_index = {r.index: r for r in references}
    topic_kw = _keywords(topic)
    stats: dict[int, Node] = {}
    best: dict[int, float] = {}

    def add(
        idx: int,
        ref: Reference,
        evidence: str,
        quality: float,
        context: str,
        section: str,
        marker: str,
        factor: float,
    ) -> None:
        node = stats.get(idx)
        if node is None:
            node = Node(
                ref_index=idx,
                title=ref.title,
                year=ref.year,
                evidence=evidence,
                context=context,
                marker=marker,
            )
            stats[idx] = node
            best[idx] = quality
        elif quality > best[idx]:
            node.evidence = evidence
            node.context = context
            node.marker = marker
            best[idx] = quality
        node.cites += 1
        node.weight += _section_weight(section) * factor
        if section:
            node.sections.add(section[:24])

    for b in blocks:
        if b.kind != "para" or not b.text:
            continue
        if _NOISE_RE.search(b.text) or _UNRELATED_RE.search(b.text):
            continue

        sup = (superscript or {}).get(b.id)
        sup_pos = _marker_pos(b.text, sup) if sup else 0
        numeric = {c.index for c in find_citations(b.text) if c.index is not None}

        for cite in find_citations(b.text):
            idx = match_index(cite, references)
            if idx is None:
                continue
            ref = by_index.get(idx)
            if not ref:
                continue
            seg, quality = _best_evidence(b.text, cite.start)
            add(
                idx,
                ref,
                seg,
                quality,
                _context_window(b.text, cite.start),
                b.section,
                cite.marker[:40],
                1.0,
            )

        if paren_numeric:
            for cite in find_paren_numeric(b.text):
                idx = match_index(cite, references)
                if idx is None or idx in numeric:
                    continue
                ref = by_index.get(idx)
                if not ref:
                    continue
                seg, quality = _best_evidence(b.text, cite.start)
                add(
                    idx,
                    ref,
                    seg,
                    quality - 0.3,
                    _context_window(b.text, cite.start),
                    b.section,
                    cite.marker[:40],
                    0.75,
                )

        # 上标数字：同一段落里已经识别过的不重复计数
        for idx in sup or ():
            if idx in numeric:
                continue
            ref = by_index.get(idx)
            if not ref:
                continue
            seg, quality = _best_evidence(b.text, sup_pos)
            add(
                idx,
                ref,
                seg,
                quality - 0.5,
                _context_window(b.text, sup_pos),
                b.section,
                str(idx),
                0.8,
            )

    for n in stats.values():
        if topic_kw:
            overlap = len(_keywords(n.title) & topic_kw) / max(len(topic_kw), 1)
            n.weight += 1.6 * overlap

    ranked = sorted(stats.values(), key=lambda n: (n.weight, n.cites), reverse=True)
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
    if _TOOL_HINT.search(text):
        return "tool"
    return "foundation"


def _build_prompt(nodes: list[Node], paper_title: str, abstract: str, method: str) -> str:
    lines = [f"本文标题：{paper_title}"]
    if abstract:
        lines.append(f"本文摘要：{abstract[:800]}")
    if method:
        lines.append(f"本文方法/贡献片段：{method[:1200]}")
    lines.append("\n候选文献（按与本文的相关度排序）：")
    for i, n in enumerate(nodes, 1):
        lines.append(f"#{i} 标题：{n.title}（{n.year or '年份未知'}）")
        sec = "、".join(sorted(n.sections)[:3]) or "未知章节"
        lines.append(f"   被引 {n.cites} 次 · 章节：{sec}")
        if n.evidence:
            lines.append(f"   引用原句：{n.evidence[:220]}")
        if n.context:
            lines.append(f"   上下文：{n.context[:300]}")
    return "\n".join(lines)


async def _label(
    nodes: list[Node], paper_title: str, abstract: str = "", method: str = ""
) -> list[Node]:
    """一次批量调用，让模型推断每篇文献的角色 + 写"借了什么/改了什么"。"""
    prompt = _build_prompt(nodes, paper_title, abstract, method)

    data: list[dict] = []
    for budget in (6000, 12000):
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
        if role == "unclear":
            continue
        if role not in ROLES:
            role = _guess_role(node.evidence, node.title)
        node.role = role
        node.borrow = (item.get("borrow") or "").strip()[:60]
        node.improve = (item.get("improve") or "").strip()[:90]
        conf = str(item.get("confidence") or "").strip().lower()
        node.confidence = conf if conf in ("high", "medium", "low") else "medium"
        ev = (item.get("evidence") or "").strip()
        # 证据必须真的出自原文，否则宁可用我们自己的原句
        if ev and ev[:16] in f"{node.evidence} {node.context}":
            node.evidence = ev[:240]

    llm_ok = bool(data)
    kept: list[Node] = []
    for i, n in enumerate(nodes, 1):
        if not llm_ok:
            n.role = _guess_role(n.evidence, n.title)
            n.borrow = n.borrow or (n.evidence[:40] + "…" if n.evidence else n.title[:30])
            n.confidence = "low"
            kept.append(n)
            continue
        if i not in seen or n.role == "unclear" or not n.borrow:
            continue
        kept.append(n)

    # 模型把话说死但一条都不留时，至少给出高权重候选，并明确标为低置信
    if not kept and nodes:
        for n in nodes[:5]:
            n.role = _guess_role(n.evidence, n.title)
            n.borrow = n.borrow or (n.evidence[:40] + "…" if n.evidence else n.title[:30])
            n.confidence = "low"
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


def _dedupe(nodes: list[Node]) -> list[Node]:
    """同一类「借了什么」只留一篇。

    只合并措辞完全一致的关系，避免把不同文献的细微差异也一并抹掉。
    """
    seen: set[tuple[str, str]] = set()
    out: list[Node] = []
    for n in sorted(nodes, key=lambda x: (x.cites, x.weight), reverse=True):
        key = re.sub(r"\s+", "", n.borrow)
        if len(key) < 8:
            out.append(n)
            continue
        sig = (n.role, key)
        if sig in seen:
            continue
        seen.add(sig)
        out.append(n)
    out.sort(key=lambda n: (n.year or 0))
    return out


async def build_lineage(blocks: list[Block], pdf_path: Path | None = None) -> dict:
    title = next((b.text for b in blocks if b.kind == "title"), "") or "未命名论文"
    years = [
        int(y)
        for y in re.findall(r"\b(20[0-4]\d)\b", " ".join(b.text for b in blocks[:40] if b.text))
    ]
    self_year = max(years) if years else None

    references = parse_references(blocks)
    abstract = next(
        (b.text for b in blocks if b.kind == "para" and (b.section or "").lower() == "abstract"),
        "",
    )
    if not abstract:
        abstract = next((b.text for b in blocks if b.kind == "para" and len(b.text) > 200), "")
    method = " ".join(
        b.text
        for b in blocks
        if b.kind == "para"
        and any(k in (b.section or "").lower() for k in ("method", "approach", "model"))
    )[:1500]

    topic = f"{title} {abstract}"
    nodes = collect(blocks, references, topic=topic)
    unique = _count_unique_citations(blocks, references)

    # 正文几乎没有可用引用标记时，识别该刊自己的编号风格
    source = "text"
    if unique < 3 and references:
        sup: dict[str, set[int]] = {}
        sup_count = 0
        if pdf_path:
            sup = _superscript_hits(pdf_path, blocks, {r.index: r for r in references})
            sup_count = len(set().union(*sup.values())) if sup else 0
        paren_count = _count_paren_citations(blocks, references)

        if paren_count > sup_count and paren_count >= 3:
            nodes = collect(blocks, references, topic=topic, paren_numeric=True)
            source = "paren"
            unique = paren_count
        elif sup:
            nodes = collect(blocks, references, topic=topic, superscript=sup)
            source = "superscript"
            unique = sup_count

    stats = {
        "references": len(references),
        "uniqueCitations": unique,
        "candidates": len(nodes),
        "source": source,
    }
    if not nodes:
        return Lineage(self_title=title, self_year=self_year, stats=stats).to_dict()

    nodes = await _label(nodes, title, abstract, method)
    nodes = _dedupe(nodes)
    stats["kept"] = len(nodes)
    return Lineage(nodes=nodes, self_title=title, self_year=self_year, stats=stats).to_dict()
