"""从论文里抽取"用户最可能卡住"的概念。

不追求 NLP 级别的概念识别，只要够导读用：
- 种子来自术语表（翻译流程已经产出，质量不错）
- 补充缩写、数据集、方法名
- 用"出现频次 + 是否在摘要/标题/图注"打分，取前 N 个
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from ..layout import Block

# 太泛的词，不适合当前置知识
_STOPWORDS = {
    "paper", "method", "methods", "model", "models", "data", "result", "results",
    "approach", "framework", "figure", "table", "section", "equation", "algorithm",
    "problem", "task", "performance", "experiment", "experiments", "analysis",
    "graph", "network", "learning", "system", "study", "work", "propose", "proposed",
    "introduction", "conclusion", "abstract", "references", "author", "authors",
    "ieee", "doi", "pdf", "figure", "eq", "et al", "e.g", "i.e",
}

_ACRONYM_RE = re.compile(r"\b[A-Z]{2,6}\b")
_DATASET_RE = re.compile(r"\b(?:ogbl|ogbn|ogbg)-[a-z0-9]+\b|\b[A-Z][A-Za-z]+(?:Net|DB)\b")

# 表格/图注里常见的大写词，不是需要解释的专业术语
_ACRONYM_STOP = {
    "DRUG", "DRUGS", "GRA", "NEW", "ALL", "EACH", "MORE", "LESS", "HIGH", "LOW",
    "YES", "NO", "ONE", "TWO", "THREE", "TYPE", "TYPES", "NUMBER", "TOTAL",
    "DATA", "SET", "SETS", "TEST", "TRAIN", "VALID", "VALUE", "VALUES",
    "FIG", "FIGS", "TAB", "TABLE", "TABLES", "THE", "AND", "FOR", "WITH", "FROM",
}

_METHOD_HINTS = ("net", "former", "gnn", "cnn", "rnn", "lstm", "bert", "gan", "vae")


@dataclass
class Concept:
    name: str            # 展示名（优先中文）
    en: str              # 英文检索词
    kind: str            # method | term | dataset | metric
    importance: float
    sections: list[str] = field(default_factory=list)
    evidence: str = ""   # 首次出现处的原文片段


def _count(term: str, texts: list[str]) -> int:
    if not term:
        return 0
    pat = re.compile(re.escape(term), re.IGNORECASE)
    return sum(len(pat.findall(t)) for t in texts)


def extract(blocks: list[Block], glossary: dict[str, str] | None = None,
            limit: int = 10) -> list[Concept]:
    """返回按重要性排序的概念列表。"""
    glossary = glossary or {}
    body = [b for b in blocks if b.kind in ("para", "caption", "heading") and b.text]
    texts = [b.text for b in body]
    full = "\n".join(texts)

    in_title = next((b.text for b in blocks if b.kind == "title"), "")
    in_abstract = ""
    for i, b in enumerate(blocks):
        if b.kind == "heading" and re.match(r"abstract|摘\s*要", b.text, re.I):
            nxt = next((x for x in blocks[i + 1:] if x.kind == "para"), None)
            in_abstract = nxt.text if nxt else ""
            break
    captions = " ".join(b.text for b in blocks if b.kind == "caption")

    found: dict[str, Concept] = {}

    def add(name: str, en: str, kind: str, weight: float) -> None:
        if not en or en.lower() in _STOPWORDS:
            return
        if en.isupper() and en.upper() in _ACRONYM_STOP:
            return
        n = _count(en, texts)
        if n < 2:
            return
        score = weight * (1.0 + min(n, 20) / 10.0)
        if en.lower() in in_title.lower():
            score += 2.0
        if en.lower() in in_abstract.lower():
            score += 1.0
        if en.lower() in captions.lower():
            score += 0.5
        if en in found:
            found[en].importance += score
            return
        found[en] = Concept(
            name=name, en=en, kind=kind, importance=score,
            sections=_sections_of(en, blocks),
            evidence=_evidence(en, blocks),
        )

    # 1) 术语表（质量最高）
    for en, zh in glossary.items():
        add(zh.strip(), en.strip(), "term", 1.0)

    # 2) 缩写（GNN / BSD / DRNL …）
    for cap, n in Counter(_ACRONYM_RE.findall(full)).most_common(40):
        if n < 3 or cap.lower() in _STOPWORDS:
            continue
        add(cap, cap, "term", 0.6)

    # 3) 数据集名
    for ds, n in Counter(_DATASET_RE.findall(full)).most_common(10):
        if n >= 2:
            add(ds, ds, "dataset", 0.5)

    # 4) 方法名（-Net / -Former / 已知网络族）
    for word, n in Counter(re.findall(r"\b[A-Z][A-Za-z0-9\-]{2,}\b", full)).most_common(60):
        low = word.lower()
        if any(h in low for h in _METHOD_HINTS) and n >= 3:
            add(word, word, "method", 0.7)

    ranked = sorted(found.values(), key=lambda c: c.importance, reverse=True)
    # 同一术语的中英文重复项去掉（保留分数高的）
    seen: set[str] = set()
    out: list[Concept] = []
    for c in ranked:
        key = c.en.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
        if len(out) >= limit:
            break
    return out


def _sections_of(term: str, blocks: list[Block]) -> list[str]:
    sections: list[str] = []
    current = ""
    pat = re.compile(re.escape(term), re.IGNORECASE)
    for b in blocks:
        if b.kind == "heading":
            current = b.text
            continue
        if b.kind == "para" and pat.search(b.text):
            label = b.section or current
            if label and label not in sections:
                sections.append(label)
    return sections[:3]


def _evidence(term: str, blocks: list[Block]) -> str:
    pat = re.compile(re.escape(term), re.IGNORECASE)
    for b in blocks:
        if b.kind != "para":
            continue
        m = pat.search(b.text)
        if m:
            start = max(0, m.start() - 60)
            return b.text[start:start + 160].strip()
    return ""
