"""导读生成：让读者循序渐进地知道"这篇论文在讲什么、要先懂哪些词"。

LLM 只负责"选哪个候选、怎么解释、怎么概括"，不允许自己造文献；
术语也必须来自论文里真实出现的候选词（翻译术语表 + 概念抽取结果）。
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

# 输出结构变更时递增，旧缓存会被自动作废重算
GUIDE_VERSION = 3

_KIND_ZH = {
    "term": "术语",
    "method": "方法",
    "dataset": "数据集",
    "metric": "指标",
    "abbr": "缩写",
}

SYS = """你是论文导读助手，目标：让一个非本领域读者在 10 分钟内知道这篇论文
"解决什么问题、怎么做、结论是什么、有哪些专业词要先懂"。

硬约束：
1. 只依据给定信息，不编造数字、结论、文献或实验设置。
2. summary ≤ 80 字，必须包含"问题 + 方法 + 主要结论"。
3. oneliner ≤ 40 字，用完全外行也能懂的话说清本文在做什么。
4. overview：problem/approach 各 ≤ 120 字；contributions/findings 各最多 4 条、
   每条 ≤ 50 字；limitations 最多 3 条，没有就留空数组。
5. terms 只能从【术语候选】里选（写候选编号 T1/T2…），6–12 条；
   zh 用领域惯用中文译名，explain ≤ 50 字，面向非本领域读者。
6. outline 3–8 条，section 必须原样使用【章节地图】里的标题；
   what 说明这一节在干什么，focus 说明阅读时关注什么。
7. prereqs 最多 5 条，只能从【概念与候选文献】里选；path 2–4 条，
   只能从【概念与候选文献】里选，按"先补概念 → 再看方法来源 → 最后读本文"排序。
8. path 每一步都必须讲清楚三件事：
   why：这篇文献与本文的具体关系（定义/方法的来源、被本文沿用、还是对照工作），
        必须写出本文哪个概念、哪一节用到它，不能只写"相关""重要"这类空话；
   order：为什么排在这个位置（为什么必须在本文之前、为什么在另一步之后）；
   gain：读完这一步能看懂本文的什么内容。
9. questions 给 3–5 个"读完后应该能回答"的问题，不要给答案。
10. 不评价论文好坏，不输出 Markdown，只输出 JSON。

输出格式：
{
  "summary": "...",
  "oneliner": "...",
  "domains": ["...", "..."],
  "difficulty": {"stars": 1-5, "minutes": 20-120, "math": "low|medium|high", "terms": "low|medium|high"},
  "overview": {
    "problem": "...",
    "approach": "...",
    "contributions": ["..."],
    "findings": ["..."],
    "limitations": ["..."]
  },
  "terms": [{"id": "T1", "zh": "...", "explain": "...", "where": "..."}],
  "outline": [{"section": "...", "what": "...", "focus": "..."}],
  "prereqs": [{"concept": "概念名", "candidate": "C1", "why": "..."}],
  "path": [
    {"candidate": "C1", "why": "本文在 XX 节用到它定义的 YY，属于概念来源", "order": "第 1 步，先补这个概念才能看懂方法", "minutes": 15, "gain": "..."}
  ],
  "questions": ["..."]
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


def _heading_ok(head: str) -> bool:
    """过滤被版面识别成标题的表格表头。"""
    words = re.findall(r"[A-Za-z][A-Za-z\-']*", head)
    if not words or len(words) > 6:
        return False
    if "/" in head or "|" in head:
        return False
    caps = sum(1 for w in words if w[:1].isupper())
    # 连续 4 个以上首字母大写、且没有小写连接词，多半是表头
    if len(words) >= 4 and caps == len(words):
        return False
    return True


def _prose_ok(text: str) -> bool:
    """判断一段文字像正文而不是表格/图注碎片。"""
    text = (text or "").strip()
    if len(text) < 70:
        return False
    if text.count(".") < 1:
        return False
    func = len(re.findall(r"\b(?:the|of|and|to|in|for|with|that|are|is|we|this|by)\b", text, re.I))
    return func >= 2


def _section_digests(blocks: list[Block], limit: int = 10) -> list[dict]:
    """每节标题 + 开头一小段原文，供模型写结构地图和主要内容。"""
    out: list[dict] = []
    active = False
    for b in blocks:
        if b.kind == "heading":
            head = (b.text or "").strip()
            if not head:
                continue
            if re.match(r"^(references?|bibliograph|参考文献)", head, re.I):
                break
            if re.match(r"^(acknowledg|appendix|supplementary)", head, re.I):
                active = False
                continue
            # 被版面切开的标题：`... paradigm for` + `scientific reasoning`
            if out and out[-1]["section"].rstrip().endswith(
                ("for", "of", "and", "the", "in", "on", "with", "to")
            ) and len(head.split()) <= 5:
                out[-1]["section"] = f"{out[-1]['section']} {head}"
                active = True
                continue
            if not _heading_ok(head):
                active = False
                continue
            if len(out) >= limit:
                break
            out.append({"section": head, "text": ""})
            active = True
        elif b.kind == "para" and active and out and b.text:
            if len(out[-1]["text"]) < 280:
                out[-1]["text"] += " " + b.text
    return [x for x in out if _prose_ok(x["text"])]


def _count_term(full: str, term: str) -> int:
    if not term or len(term) < 2:
        return 0
    pat = re.escape(term)
    return len(re.findall(rf"(?<![A-Za-z0-9]){pat}(?![A-Za-z0-9])", full, re.I))


def _term_candidates(
    glossary: dict[str, str], concepts: list[Concept], blocks: list[Block], limit: int = 28
) -> list[dict]:
    """术语候选：翻译术语表优先（按出现频次排序），其次是从正文抽出的概念。"""
    full = "\n".join(b.text for b in blocks if b.text)
    seen: set[str] = set()
    out: list[dict] = []

    def push(en: str, zh: str, kind: str, sections: list[str], evidence: str) -> None:
        en = (en or "").strip()
        key = en.lower()
        if not key or key in seen or _count_term(full, en) < 2:
            return
        seen.add(key)
        out.append(
            {
                "en": en,
                "zh": (zh or "").strip(),
                "kind": kind or "term",
                "sections": (sections or [])[:3],
                "evidence": (evidence or "").strip()[:160],
            }
        )

    ranked_glossary = sorted(
        (glossary or {}).items(), key=lambda kv: _count_term(full, kv[0]), reverse=True
    )
    for en, zh in ranked_glossary[:20]:
        push(en, zh, "term", [], "")
    for c in concepts:
        zh = (glossary or {}).get(c.en, "") or (c.name if c.name != c.en else "")
        push(c.en, zh, c.kind, c.sections, c.evidence)
    return out[:limit]


def _build_prompt(
    title: str,
    abstract: str,
    headings: list[str],
    digests: list[dict],
    concepts: list[Concept],
    candidates: list[Candidate],
    terms: list[dict],
) -> str:
    by_concept: dict[str, list[Candidate]] = {}
    for c in candidates:
        by_concept.setdefault(c.concept.name, []).append(c)

    idx = 0
    prereq_lines: list[str] = []
    for concept in concepts:
        group = by_concept.get(concept.name) or []
        if not group:
            continue
        prereq_lines.append(f"【概念】{concept.name}（{concept.en}，{concept.kind}）")
        if concept.sections:
            prereq_lines.append(f"  出现章节：{'、'.join(concept.sections)}")
        for cand in group:
            idx += 1
            cand.uid = f"C{idx}"
            r = cand.ref
            prereq_lines.append(f"  C{idx}. {r.title}（{r.year or '年份未知'}）")
        prereq_lines.append("")

    term_lines: list[str] = []
    for i, t in enumerate(terms, 1):
        zh = f"（{t['zh']}）" if t["zh"] else ""
        sec = f" · 章节：{'、'.join(t['sections'])}" if t["sections"] else ""
        term_lines.append(f"T{i}. {t['en']}{zh} · {_KIND_ZH.get(t['kind'], t['kind'])}{sec}")
        if t["evidence"]:
            term_lines.append(f"   原文语境：{t['evidence'][:110]}")

    section_lines = []
    for d in digests:
        text = d["text"][:260] if d["text"] else ""
        section_lines.append(f"- {d['section']}：{text}")

    return "\n".join(
        [
            f"【标题】{title}",
            f"【摘要】{abstract}",
            f"【章节】{' / '.join(headings)}",
            "",
            "【章节地图（标题 + 开头原文）】",
            *(section_lines or ["（没有识别到章节）"]),
            "",
            "【术语候选（只能从中选，用编号引用）】",
            *(term_lines or ["（没有抽取到术语候选）"]),
            "",
            "【概念与候选文献（均来自本文参考文献）】",
            *(prereq_lines or ["（没有找到可推荐的前置文献）"]),
        ]
    )


def _strip_json(text: str) -> dict:
    """从模型回复里取 JSON；兼容代码块、前后解释、尾随逗号。"""
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip()).strip()
    try:
        return json.loads(t)
    except Exception:
        pass

    # 不用贪婪的 r"\{.*\}"：回复里出现多个花括号时会把整段吞进去。
    # 从每个 '{' 开始做括号配对，取第一个能解析的对象。
    for start in [m.start() for m in re.finditer(r"\{", t)]:
        depth = 0
        in_str = False
        escaped = False
        for i in range(start, len(t)):
            ch = t[i]
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = t[start : i + 1]
                    for attempt in (candidate, re.sub(r",\s*([}\]])", r"\1", candidate)):
                        try:
                            return json.loads(attempt)
                        except Exception:
                            continue
                    break
    return {}


def _as_list(value, limit: int, width: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for v in value:
        if isinstance(v, str) and v.strip():
            out.append(v.strip()[:width])
        if len(out) >= limit:
            break
    return out


def _to_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _norm_head(text: str) -> str:
    s = re.sub(r"^[\d.\s]+", "", (text or "").strip()).lower()
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", s)


def _match_heading(section: str, headings: list[str]) -> str:
    target = _norm_head(section)
    if not target:
        return ""
    for head in headings:
        norm = _norm_head(head)
        if not norm:
            continue
        if norm == target or (len(target) >= 4 and target in norm) or (len(norm) >= 4 and norm in target):
            return head
    return ""


def _fallback_terms(terms: list[dict], limit: int = 8) -> list[dict]:
    out: list[dict] = []
    for t in terms[:limit]:
        out.append(
            {
                "en": t["en"],
                "zh": t["zh"],
                "kind": t["kind"],
                "explain": "",
                "where": "、".join(t["sections"][:2]),
            }
        )
    return out


def _fallback_outline(digests: list[dict], limit: int = 8) -> list[dict]:
    out: list[dict] = []
    for d in digests[:limit]:
        text = re.sub(r"\s+", " ", d["text"]).strip()
        out.append({"section": d["section"], "what": text[:90], "focus": ""})
    return out


def _ref_reason(cand: Candidate, year: int | None, step: int) -> tuple[str, str, str]:
    """模型没给推荐理由时，用「本文哪一节用到什么概念 + 年份」拼出可核查的理由。"""
    concept = cand.concept
    section = (concept.sections or ["正文"])[0]
    if cand.ref.year and year and cand.ref.year < year:
        when = f"它发表于 {cand.ref.year} 年，属于本文之前的基础工作"
    elif cand.ref.year:
        when = f"它发表于 {cand.ref.year} 年，是本文引用过的相关工作"
    else:
        when = "它是本文参考文献里被引用过的相关工作"
    why = f"本文在「{section}」中直接用到「{concept.name}」，这篇是它的来源或同源工作，先读能补上概念的定义与用法"
    order = f"第 {step} 步读：先对「{concept.name}」建立基本理解，再进入本文方法"
    gain = f"读懂「{concept.name}」是什么、本文为什么需要它；{when}"
    return why[:140], order[:110], gain[:100]


def _fallback_path(candidates: list[Candidate], year: int | None, limit: int = 2) -> list[dict]:
    """没有可用的模型 path 时，按匹配分数给出 1–2 篇前置文献，并附带理由。"""
    out: list[dict] = []
    used: set[int] = set()
    for cand in sorted(candidates, key=lambda c: c.score, reverse=True):
        if cand.ref.index in used:
            continue
        used.add(cand.ref.index)
        why, order, gain = _ref_reason(cand, year, len(out) + 1)
        out.append(
            {
                "kind": "ref",
                "title": cand.ref.title,
                "year": cand.ref.year,
                "concept": cand.concept.name,
                "minutes": 20,
                "why": why,
                "order": order,
                "gain": gain,
            }
        )
        if len(out) >= limit:
            break
    return out


def _self_step(minutes: int = 30) -> dict:
    return {
        "kind": "paper",
        "title": "本文",
        "minutes": minutes,
        "why": "前面几步只补概念和背景，本文才是要精读的对象：看它如何把这些概念落到自己的问题上。",
        "order": "最后读：带着已经补好的概念进正文，重点读方法与实验，遇到没解释的术语再回看前面的文献。",
        "gain": "完整理解本文的问题、做法和结论。",
    }


def _fallback(
    title: str,
    concepts: list[Concept],
    candidates: list[Candidate],
    year: int | None,
    terms: list[dict] | None = None,
    digests: list[dict] | None = None,
) -> dict:
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

    summary = (
        f"本文围绕「{concepts[0].name}」展开。"
        if concepts
        else f"本文讨论了「{title}」。"
    )
    return {
        "version": GUIDE_VERSION,
        "status": "ready",
        "summary": summary,
        "oneliner": summary,
        "domains": [],
        "difficulty": {"stars": 3, "minutes": 30, "math": "medium", "terms": "medium"},
        "overview": {
            "problem": summary,
            "approach": "",
            "contributions": [],
            "findings": [],
            "limitations": [],
        },
        "glossary": _fallback_terms(terms or []),
        "outline": _fallback_outline(digests or []),
        "questions": [],
        "prereqs": prereqs,
        "path": _fallback_path(candidates, year) + [_self_step(30)],
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
    digests = _section_digests(blocks)
    headings = [d["section"] for d in digests] or _headings(blocks)
    year = _paper_year(blocks)
    glossary = glossary or {}

    concepts = extract(blocks, glossary, limit=12)
    terms = _term_candidates(glossary, concepts, blocks)
    if not abstract and not terms and not concepts:
        return {
            **_fallback(title, [], [], year, terms, digests),
            "summary": "没有抽取到可用的内容。",
            "oneliner": "没有抽取到可用的内容。",
        }

    references = parse_references(blocks)
    candidates = match(concepts, references, paper_year=year) if concepts else []

    prompt = _build_prompt(title, abstract, headings, digests, concepts, candidates, terms)

    data: dict = {}
    # 推理模型的 reasoning token 也计入 max_tokens；导读 prompt 本身较长，
    # V4.1 Flash / K3.1 Pro 这类思考模型给 3000 很容易只思考不出 JSON。
    for budget in (6000, 12000):
        try:
            raw = await chat(
                [{"role": "system", "content": SYS}, {"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=budget,
            )
            data = _strip_json(raw)
            if isinstance(data.get("guide"), dict):
                data = data["guide"]
            if data.get("summary"):
                break
            log.warning(
                "导读 JSON 解析失败（max_tokens=%d，回复 %d 字符）：%r",
                budget,
                len(raw),
                raw[:160],
            )
        except Exception as e:
            log.warning("导读生成失败（max_tokens=%d）：%s", budget, e)

    if not data.get("summary"):
        return _fallback(title, concepts, candidates, year, terms, digests)

    uid2cand = {c.uid: c for c in candidates if c.uid}

    raw_prereqs = data.get("prereqs")
    if not isinstance(raw_prereqs, list):
        raw_prereqs = []
    prereqs = []
    for p in raw_prereqs[:5]:
        if not isinstance(p, dict):
            continue
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

    difficulty = data.get("difficulty") if isinstance(data.get("difficulty"), dict) else {}
    minutes = _to_int(difficulty.get("minutes"), 30)
    picked = []
    raw_path = data.get("path")
    if not isinstance(raw_path, list):
        raw_path = []
    for step in raw_path[:4]:
        if not isinstance(step, dict):
            continue
        cand = uid2cand.get(str(step.get("candidate", "")).strip())
        if not cand:
            continue
        # 模型没写理由时也要有理由：用概念、章节、年份兜底
        why, order, gain = _ref_reason(cand, year, len(picked) + 1)
        picked.append(
            {
                "kind": "ref",
                "title": cand.ref.title,
                "year": cand.ref.year,
                "concept": cand.concept.name,
                "minutes": _to_int(step.get("minutes"), 20),
                "why": (step.get("why") or "").strip()[:140] or why,
                "order": (step.get("order") or "").strip()[:110] or order,
                "gain": (step.get("gain") or "").strip()[:100] or gain,
            }
        )
    if not picked:
        picked = _fallback_path(candidates, year)
    path = picked + [_self_step(minutes)]

    # 术语必须来自候选：模型用 T 编号引用，避免凭空造词
    term_by_id = {f"T{i}": t for i, t in enumerate(terms, 1)}
    glossary_out: list[dict] = []
    seen_en: set[str] = set()
    raw_terms = data.get("terms")
    if not isinstance(raw_terms, list):
        raw_terms = []
    for item in raw_terms[:12]:
        if not isinstance(item, dict):
            continue
        tid = str(item.get("id", "")).strip().upper()
        cand = term_by_id.get(tid)
        if not cand:
            # 也允许模型直接回填英文词
            en = str(item.get("en", "")).strip().lower()
            cand = next((t for t in terms if t["en"].lower() == en), None)
        if not cand:
            continue
        key = cand["en"].lower()
        if key in seen_en:
            continue
        seen_en.add(key)
        glossary_out.append(
            {
                "en": cand["en"],
                "zh": (item.get("zh") or cand["zh"] or "").strip()[:40],
                "kind": cand["kind"],
                "explain": (item.get("explain") or "").strip()[:80],
                "where": (item.get("where") or "、".join(cand["sections"][:2]))[:60],
            }
        )
    if len(glossary_out) < 4:
        for t in _fallback_terms(terms):
            if t["en"].lower() in seen_en:
                continue
            glossary_out.append(t)
            if len(glossary_out) >= 6:
                break

    actual_heads = [d["section"] for d in digests] or headings
    outline: list[dict] = []
    raw_outline = data.get("outline")
    if not isinstance(raw_outline, list):
        raw_outline = []
    for item in raw_outline[:8]:
        if not isinstance(item, dict):
            continue
        section = _match_heading(str(item.get("section", "")), actual_heads)
        if not section:
            continue
        outline.append(
            {
                "section": section,
                "what": (item.get("what") or "").strip()[:100],
                "focus": (item.get("focus") or "").strip()[:100],
            }
        )
    if not outline:
        outline = _fallback_outline(digests)

    raw_overview = data.get("overview") if isinstance(data.get("overview"), dict) else {}
    overview = {
        "problem": str(raw_overview.get("problem") or "")[:160],
        "approach": str(raw_overview.get("approach") or "")[:160],
        "contributions": _as_list(raw_overview.get("contributions"), 4, 80),
        "findings": _as_list(raw_overview.get("findings"), 4, 80),
        "limitations": _as_list(raw_overview.get("limitations"), 3, 80),
    }

    raw_domains = data.get("domains")
    if not isinstance(raw_domains, list):
        raw_domains = []

    return {
        "version": GUIDE_VERSION,
        "status": "ready",
        "summary": (data.get("summary") or "")[:120],
        "oneliner": (data.get("oneliner") or "")[:60],
        "domains": [d for d in raw_domains if isinstance(d, str)][:4],
        "difficulty": difficulty,
        "overview": overview,
        "glossary": glossary_out,
        "outline": outline,
        "questions": _as_list(data.get("questions"), 5, 100),
        "prereqs": prereqs,
        "path": path,
        "year": year,
        "concepts": [
            {"name": c.name, "en": c.en, "kind": c.kind, "sections": c.sections}
            for c in concepts
        ],
    }
