"""前置知识匹配：概念 → 本文引用过的、更早的、能对上的那几篇。

设计取舍：
- 只从**本文自己的参考文献**里选，不查外部库。理由：前置知识必须"本文真的建立在它之上"，
  全局检索命中率低、还容易推荐不相关的高被引论文（实测搜 "regulation type" 会返回癌症综述）。
- 宁可少推，也不推错：一个概念找不到匹配就不给前置，只在导读里保留概念名。
- OpenAlex 检索留到 P1 的脉络图再启用（那里需要引用关系，不是文本匹配）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .concepts import Concept
from .references import Reference

_REVIEW_HINT = re.compile(r"survey|review|overview|tutorial|benchmark", re.IGNORECASE)


@dataclass
class Candidate:
    concept: Concept
    ref: Reference
    score: float
    uid: str = ""       # 给 LLM 的候选编号 C1/C2…

    def to_dict(self) -> dict:
        return {
            "concept": self.concept.name,
            "en": self.concept.en,
            "kind": self.concept.kind,
            "paper": self.ref.to_dict(),
            "score": round(self.score, 3),
        }


def _tokens(term: str) -> list[str]:
    return [t for t in re.findall(r"[A-Za-z][A-Za-z\-]{3,}", term) if t]


def _match_score(concept: Concept, ref: Reference, paper_year: int | None) -> float:
    hay = f"{ref.title} {ref.raw}".lower()
    en = concept.en.lower().strip()
    if not en:
        return 0.0

    score = 0.0
    if en in hay:                                  # 整词命中
        score = 1.0
    else:
        toks = _tokens(concept.en)
        hit = sum(1 for t in toks if t.lower() in hay)
        if toks and hit == len(toks) and hit > 0:
            score = 0.7
        elif hit >= 2:
            score = 0.45
        else:
            return 0.0

    if paper_year and ref.year and ref.year < paper_year:
        score += 0.25                              # 比本文早，才可能是前置
    if _REVIEW_HINT.search(ref.title):
        score += 0.15
    if concept.kind == "method":
        score += 0.05
    return score


def match(
    concepts: list[Concept],
    references: list[Reference],
    paper_year: int | None = None,
    per_concept: int = 2,
) -> list[Candidate]:
    """给每个概念挑 1–2 篇本文引用过的前置文献。"""
    if not references:
        return []

    out: list[Candidate] = []
    used: set[int] = set()
    for concept in concepts:
        scored = [(r, _match_score(concept, r, paper_year)) for r in references]
        scored = [(r, s) for r, s in scored if s > 0 and r.index not in used]
        scored.sort(key=lambda x: x[1], reverse=True)
        for ref, s in scored[:per_concept]:
            used.add(ref.index)
            out.append(Candidate(concept=concept, ref=ref, score=s))
    return out
