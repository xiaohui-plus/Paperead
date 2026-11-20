"""论文档案：把一篇论文一次性整理成可复用的问答材料。

为什么不每次检索：
- 论文入库后 blocks 不再变化，每次提问都重新建索引 + 让模型改写检索词 + 多轮
  工具调用，既慢（多一次或几次模型往返）又不稳定（召回不到就答偏）；
- 这里把「章节大纲 + 摘要 + 全篇段落 + 图表/公式清单」整理成档案，进程内缓存，
  每次问答复用。正文超出预算时保证**每段至少出现一次**（列首句），再用本地
  BM25 挑出与问题最相关的段落展开全文，全程不调用模型。

档案只负责「材料」，讲法与规则由 qa.ANSWER_SYS 约束。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..layout import Block
from .qa import BM25, tokenize

# 一次问答愿意给论文正文的字符预算。中文 1 字≈1 token，26k 约等于 2 万 token 量级，
# 对主流 32k/64k 上下文模型留出了历史、引用与回答的空间。
DEFAULT_BUDGET = 26_000
ABSTRACT_LIMIT = 1_600
OUTLINE_LIMIT = 1_200
FIGURE_LIMIT = 1_200
EQUATION_LIMIT = 900
REFERENCE_LIMIT = 1_800
SNIPPET_LIMIT = 180

_BODY_KINDS = ("para", "abstract", "keywords", "caption", "figure", "table", "equation")
_SKIP_KINDS = ("margin", "affiliation", "author", "title")
_SENT_SPLIT = re.compile(r"(?<=[。！？!?;；])|(?<=\.)\s+")

# 作者简介、版权声明这类段落对问答没有价值，且容易被问题误命中
_NOISE_RE = re.compile(
    r"开放获取|知识共享|creative commons|all rights reserved|downloaded from|"
    r"作者简介|通讯作者|基金项目|E-?mail|邮箱|基金资助",
    re.IGNORECASE,
)


@dataclass
class Focus:
    """用于前端出处 chips 与重点段落展开的命中。"""

    block_id: str
    page: int
    label: str
    snippet: str
    score: float = 0.0


@dataclass
class Chunk:
    block_id: str
    order: int
    page: int
    kind: str
    section: str
    text: str
    translated: str
    caption: str = ""
    latex: str = ""
    eq_number: str = ""

    @property
    def display(self) -> str:
        return (self.translated or self.text or "").strip()

    @property
    def source(self) -> str:
        return (self.text or "").strip()

    def label(self) -> str:
        section = _clip(self.section, 36)
        return f"P{self.page} §{section}" if section else f"P{self.page}"


@dataclass
class Dossier:
    paper_id: str
    title: str
    authors: str
    pages: int
    chunks: list[Chunk] = field(default_factory=list)
    outline: list[tuple[str, int]] = field(default_factory=list)
    abstract: str = ""
    keywords: str = ""
    figures: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    equations: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    chars: int = 0
    body_chars: int = 0

    @property
    def body(self) -> list[Chunk]:
        return [c for c in self.chunks if c.kind in _BODY_KINDS]

    def focus(self, question: str, quotes_text: str = "", k: int = 6) -> list[Focus]:
        """本地 BM25 定位，毫秒级；不调用模型，也不替代全篇材料。"""
        docs = self.body
        if not docs:
            return []
        query = tokenize(f"{question} {quotes_text}")
        # 原文与译文各建一份索引再取较大值：中文问题打译文、英文问题打原文，
        # 只打译文会让英文提问在已翻译的论文上一分都拿不到。
        en_scores = BM25([tokenize(c.source) for c in docs]).score(query)
        zh_scores = BM25([tokenize(c.display) for c in docs]).score(query)
        scores = [max(en_scores[i], zh_scores[i]) for i in range(len(docs))]
        ranked = sorted(zip(docs, scores), key=lambda item: item[1], reverse=True)
        out: list[Focus] = []
        per_page: dict[int, int] = {}
        for chunk, score in ranked:
            if score <= 0:
                break
            if per_page.get(chunk.page, 0) >= 2:
                continue
            per_page[chunk.page] = per_page.get(chunk.page, 0) + 1
            out.append(
                Focus(
                    block_id=chunk.block_id,
                    page=chunk.page,
                    label=f"P{chunk.page}",
                    snippet=_clip(chunk.display, SNIPPET_LIMIT),
                    score=score,
                )
            )
            if len(out) >= k:
                break
        return out

    def render(
        self,
        question: str,
        quotes: list[str] | None = None,
        budget: int = DEFAULT_BUDGET,
    ) -> tuple[str, list[Focus]]:
        """渲染成问答材料：[P页 §章节] 原文，超预算时全篇覆盖 + 重点段落全文。"""
        quotes = [q for q in (quotes or []) if q.strip()]
        quote_text = " ".join(quotes)
        hits = self.focus(question, quote_text, 6)

        head = [
            f"【论文档案】《{self.title or '未命名'}》"
            + (f" · {self.authors}" if self.authors else "")
            + (f" · 共 {self.pages} 页" if self.pages else "")
            + f" · 正文 {len(self.body)} 段"
        ]
        # 头部（大纲/摘要/图表/公式/参考文献）最多占预算的 38%，避免清单把正文挤没
        head_budget = max(1_800, int(budget * 0.38))
        head_len = len(head[0])

        def add(part: str) -> None:
            nonlocal head_len
            if not part:
                return
            room = head_budget - head_len
            if room <= 0:
                return
            if len(part) > room:
                part = _clip(part, room)
            head.append(part)
            head_len += len(part)

        if self.outline:
            lines = ["【章节大纲】"]
            budget_left = OUTLINE_LIMIT
            for title, page in self.outline:
                line = f"- {_clip(title, 60)}（第 {page} 页）"
                if budget_left <= 0:
                    lines.append("- …（大纲较长，后续章节省略）")
                    break
                lines.append(line)
                budget_left -= len(line)
            add("\n".join(lines))
        if self.keywords:
            add(f"【关键词】{_clip(self.keywords, 300)}")
        if self.abstract:
            add("【摘要】\n" + _clip(self.abstract, ABSTRACT_LIMIT))
        if self.figures:
            add("【图表清单】\n" + "\n".join(self.figures[:24]))
        if self.tables:
            add("【表格清单】\n" + "\n".join(self.tables[:16]))
        if self.equations:
            add("【主要公式】\n" + "\n".join(self.equations[:12]))
        if self.references:
            add(
                "【参考文献（节选，仅用于核对编号）】\n"
                + "\n".join(self.references[:20])
            )

        head_text = "\n\n".join(head)
        body_budget = max(1_200, budget - len(head_text) - 400)

        # 各段正文总量：用原文，避免把机器译文当成论文原话
        full = [c for c in self.body if c.source]
        full_chars = sum(len(c.source) for c in full)
        rendered: list[str] = []

        if full_chars <= body_budget:
            for c in full:
                rendered.append(f"[{c.label()}] {c.source}")
            mode = "全文"
        else:
            # 节选模式：先保证全篇覆盖（每段首句），再挑相关问题段落展开
            # 每段即使被省略也要留一个短标签，先把这部分固定成本从预算里扣掉
            placeholder_cost = sum(len(f"[{c.label()}] …") + 2 for c in full)
            usable = max(1_000, body_budget - placeholder_cost)
            brief_budget = int(usable * 0.55)
            focus_budget = usable - brief_budget
            per = max(90, min(220, brief_budget // max(1, len(full))))
            brief_budget -= per
            for c in full:
                if brief_budget <= 0:
                    rendered.append(f"[{c.label()}] …")
                    continue
                snippet = _first_sentence(c.source, per)
                rendered.append(f"[{c.label()}] {snippet}")
                brief_budget -= len(snippet)
            by_id = {c.block_id: c for c in full}
            expanded: set[str] = set()
            for hit in hits:
                if focus_budget <= 0:
                    break
                chunk = by_id.get(hit.block_id)
                if chunk is None or chunk.block_id in expanded:
                    continue
                expanded.add(chunk.block_id)
                body = _clip(chunk.source, min(1_400, focus_budget))
                if not body:
                    continue
                rendered.append(f"[{chunk.label()} · 全文] {body}")
                focus_budget -= len(body)
            mode = "节选"

        if mode == "节选":
            rendered.insert(
                0,
                "【全篇正文（节选模式）】为保证整篇论文都在材料里，下面每段至少列出首句；"
                "标注「全文」的是与本次问题最相关的段落。未标「全文」的段落如需细节，"
                "请说明现有材料只到首句，不要臆测展开。",
            )
        else:
            rendered.insert(0, "【全篇正文（全文）】按页顺序排列，可直接引用。")

        text = head_text + "\n\n" + "\n\n".join(rendered)
        return text, hits


def _clip(text: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", (text or "").strip())
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def _first_sentence(text: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", (text or "").strip())
    if len(value) <= limit:
        return value
    cut = value[:limit]
    match = list(_SENT_SPLIT.finditer(cut))
    if match and match[-1].start() > limit * 0.4:
        return cut[: match[-1].start() + 1].strip()
    return cut.rstrip() + "…"


def _is_noise(text: str) -> bool:
    return bool(_NOISE_RE.search(text or ""))


def build_dossier(paper_id: str, paper: dict | None, blocks: list[dict]) -> Dossier:
    """把 blocks 整理成档案。纯函数，缓存由调用方负责。"""
    paper = paper or {}
    ordered = sorted(blocks or [], key=lambda b: b.get("order", 0))
    chunks: list[Chunk] = []
    outline: list[tuple[str, int]] = []
    abstract_parts: list[str] = []
    keywords = ""
    figures: list[str] = []
    tables: list[str] = []
    equations: list[str] = []
    references: list[str] = []

    for raw in ordered:
        kind = str(raw.get("kind") or "")
        if kind in _SKIP_KINDS:
            continue
        text = (raw.get("text") or "").strip()
        translated = (raw.get("translated") or "").strip()
        caption = (raw.get("caption") or "").strip()
        latex = (raw.get("latex") or "").strip()
        # 公式块常常 text 为空、只有 latex；不能在这个空判断里提前丢掉
        if not any((text, translated, caption, latex)):
            continue
        if kind in ("para", "abstract", "keywords") and _is_noise(text):
            continue

        page = int(raw.get("page") or 0)
        section = str(raw.get("section") or "")

        if kind == "heading":
            title = _clip(text, 80)
            if title:
                outline.append((title, page))
            continue

        if kind == "abstract":
            abstract_parts.append(text or translated)
            continue

        if kind == "keywords":
            keywords = _clip(f"{text} {translated}".strip(), 300)
            continue

        if kind in ("figure", "table"):
            label = "图" if kind == "figure" else "表"
            line = f"- {label}{f'：{_clip(caption, 140)}' if caption else ''}（第 {page} 页）"
            (figures if kind == "figure" else tables).append(line)
            continue

        if kind == "equation":
            tex = (raw.get("latex") or "").strip()
            if tex:
                num = str(raw.get("eq_number") or "").strip()
                equations.append(f"- {f'({num}) ' if num else ''}${_clip(tex, 200)}$")
            continue

        if kind == "reference":
            if len(references) < 20:
                references.append(f"- {_clip(text, 220)}")
            continue

        if kind not in _BODY_KINDS:
            continue

        chunks.append(
            Chunk(
                block_id=str(raw.get("id") or ""),
                order=int(raw.get("order") or 0),
                page=page,
                kind=kind,
                section=section,
                text=text,
                translated=translated,
                caption=caption,
                latex=str(raw.get("latex") or ""),
                eq_number=str(raw.get("eq_number") or ""),
            )
        )

    body_chars = sum(len(c.source) for c in chunks if c.kind in _BODY_KINDS)
    return Dossier(
        paper_id=paper_id,
        title=str(paper.get("title") or "").strip(),
        authors=str(paper.get("authors") or "").strip(),
        pages=int(paper.get("pages") or 0),
        chunks=chunks,
        outline=outline,
        abstract="\n".join(abstract_parts),
        keywords=keywords,
        figures=figures,
        tables=tables,
        equations=equations,
        references=references,
        chars=sum(len(c.source) + len(c.translated) for c in chunks),
        body_chars=body_chars,
    )


def format_quotes(quotes: list[dict]) -> str:
    """用户划词引用的统一材料格式（阅读页提问入口共用）。"""
    parts: list[str] = []
    for quote in (quotes or [])[:8]:
        text = re.sub(r"\s+", " ", str(quote.get("text") or "")).strip()
        if not text:
            continue
        page = quote.get("page")
        where = f" · 第 {page} 页" if page else ""
        source = str(quote.get("source") or "")
        kind = "译文" if source == "zh" else "原文"
        if source == "pdf":
            kind = "原版 PDF"
        parts.append(f"### 用户选中的{kind}片段{where}\n> {_clip(text, 1_200)}")
    if not parts:
        return ""
    return (
        "【用户引用】本次问题优先针对下面的片段；回答要先回应它，再补充论文其他部分：\n\n"
        + "\n\n".join(parts)
    )