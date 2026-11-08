"""论文问答：论文内检索（默认）+ 外部检索升级 + 带出处的回答。

为什么不是纯 Agent：
- 80% 的问题都能在论文里找到答案，走固定管道只需 1 次检索 + 1 次生成（≈4s），
  而 Agent 要 3–8 轮工具调用（≈15s），还可能"忘了读论文"。
为什么不是纯 RAG：
- "这个方向 2025 年有没有新进展" 这类问题，论文里根本没有，必须出去找。

所以：先论文内检索；分数低 / 问题明确问外部时，才升级到外部检索。
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from ..layout import Block
from ..llm import chat, chat_stream

log = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z\-]{1,}|[\u4e00-\u9fa5]")
_STOP = {
    "the", "a", "an", "of", "and", "or", "to", "in", "for", "on", "with", "is",
    "are", "was", "were", "be", "been", "this", "that", "these", "those", "it",
    "as", "by", "at", "from", "we", "our", "can", "may", "not", "but", "which",
    "的", "了", "是", "在", "和", "与", "对", "把", "被", "这", "那", "有", "也",
}

_NOISE_SECTION_RE = re.compile(
    r"acknowledg|致\s*谢|author|作者|funding|基金|conflict|利益冲突|about the authors",
    re.IGNORECASE,
)
_EXTERNAL_RE = re.compile(
    r"最新|近几年|后续|被超越|有没有.*(新|更好)|sota|state of the art|对比其他|其他工作|"
    r"领域内|发展趋势|和.*比|比.*好吗|202[4-9]|202[0-9]年",
    re.IGNORECASE,
)
_DIALOGUE_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass
class Doc:
    block_id: str
    page: int
    order: int
    text: str          # 原文
    translated: str    # 译文（可能为空）
    tokens: list[str] = field(default_factory=list)


# 作者简介、版权声明、致谢：检索时会被误命中，直接排除
_NOISE_RE = re.compile(
    r"开放获取|知识共享|creative commons|author biography|"
    r"is (?:a|an) (?:professor|associate professor|phd|researcher|student)|"
    r"研究领域|作者简介|通讯作者|基金项目|all rights reserved|downloaded from|"
    r"教授|副教授|博士生导师|研究方向|E-?mail|邮箱|基金资助|"
    r"(?:大学|学院|实验室|研究所).{0,20}(?:讲师|教授|研究员|博士)",
    re.IGNORECASE,
)


class BM25:
    """轻量 BM25；中英混排按字符/词切分，够用且零依赖。"""

    def __init__(self, docs: list[list[str]], k1: float = 1.4, b: float = 0.72):
        self.k1, self.b = k1, b
        self.n = len(docs)
        self.df: dict[str, int] = {}
        self.tf: list[dict[str, int]] = []
        self.len: list[int] = []
        for toks in docs:
            counter: dict[str, int] = {}
            for t in toks:
                counter[t] = counter.get(t, 0) + 1
            self.tf.append(counter)
            self.len.append(len(toks))
            for t in counter:
                self.df[t] = self.df.get(t, 0) + 1
        self.avg = (sum(self.len) / self.n) if self.n else 0.0

    def score(self, query: list[str]) -> list[float]:
        out = [0.0] * self.n
        if not self.n or not query:
            return out
        for q in set(query):
            df = self.df.get(q, 0)
            if not df:
                continue
            idf = math.log(1 + (self.n - df + 0.5) / (df + 0.5))
            for i in range(self.n):
                f = self.tf[i].get(q, 0)
                if not f:
                    continue
                dl = self.len[i] or 1
                out[i] += idf * (f * (self.k1 + 1)) / (f + self.k1 * (1 - self.b + self.b * dl / self.avg))
        return out


def tokenize(text: str) -> list[str]:
    """英文按词，中文按二元组——单字匹配会把"章""文"这种高频字算成相关，
    二元组（核心/心创/创新）才能区分"核心创新点"和"这篇文章"。"""
    text = text or ""
    toks = [t.lower() for t in re.findall(r"[A-Za-z][A-Za-z\-]{1,}", text)]
    toks = [t for t in toks if t not in _STOP]

    for run in re.findall(r"[\u4e00-\u9fa5]+", text):
        if len(run) == 1:
            toks.append(run)
            continue
        for i in range(len(run) - 1):
            bg = run[i : i + 2]
            if bg not in _STOP:
                toks.append(bg)
    return toks


def build_index(blocks: list[Block]) -> tuple[list[Doc], BM25, BM25]:
    docs: list[Doc] = []
    for b in blocks:
        # 只索引正文段落：标题/图注太短，任何问题都能"命中"，会污染来源
        if b.kind != "para":
            continue
        body = b.text or b.translated or ""
        if len(body) < 120:
            continue
        if _NOISE_RE.search(body) or (b.translated and _NOISE_RE.search(b.translated)):
            continue
        if _NOISE_SECTION_RE.search(b.section or ""):
            continue
        docs.append(
            Doc(
                block_id=b.id,
                page=b.page,
                order=b.order,
                text=b.text or "",
                translated=b.translated or "",
            )
        )

    en = BM25([tokenize(d.text) for d in docs])
    zh = BM25([tokenize(d.translated) for d in docs])
    return docs, en, zh


def retrieve(docs: list[Doc], en: BM25, zh: BM25, query: str, k: int = 6) -> list[tuple[Doc, float]]:
    q_en = tokenize(query)
    q_zh = tokenize(query)
    # 中文问题直接对译文打分；英文关键词对原文打分
    s_en = en.score(q_en)
    s_zh = zh.score(q_zh) if any("\u4e00" <= c <= "\u9fff" for c in query) else [0.0] * len(docs)
    merged = [(docs[i], max(s_en[i], s_zh[i])) for i in range(len(docs))]
    merged.sort(key=lambda x: x[1], reverse=True)

    # 同一页最多取 2 段，避免整屏都是同一页的内容
    out: list[tuple[Doc, float]] = []
    per_page: dict[int, int] = {}
    for doc, score in merged:
        if score <= 0:
            break
        if per_page.get(doc.page, 0) >= 2:
            continue
        per_page[doc.page] = per_page.get(doc.page, 0) + 1
        out.append((doc, score))
        if len(out) >= k:
            break
    return out


async def expand_query(question: str) -> list[str]:
    """把中文问题扩成英文检索词，提升对英文原文的召回。失败就返回原问题。"""
    try:
        raw = await chat(
            [
                {
                    "role": "system",
                    "content": (
                        "把用户关于论文的问题改写成用于检索的英文关键词（3-8 个），"
                        "以及中文关键词（3-8 个）。只输出 JSON："
                        '{"en": ["..."], "zh": ["..."]}'
                    ),
                },
                {"role": "user", "content": question},
            ],
            temperature=0.1,
            max_tokens=600,
        )
    except Exception as e:
        log.info("查询改写失败：%s", e)
        return []

    data = _parse_json(raw)
    if not isinstance(data, dict):
        return []
    words: list[str] = []
    for key in ("en", "zh"):
        for w in data.get(key) or []:
            if isinstance(w, str) and w.strip():
                words.append(w.strip())
    return words


async def rewrite_and_expand(
    question: str, history: list[dict] | None = None
) -> tuple[str, list[str]]:
    """把追问改写成独立问题，并给出中英检索词。一次调用完成两件事。

    没有历史时退化为原来的关键词扩张；解析失败时 standalone 用原问题。
    """
    if not history:
        return question, await expand_query(question)

    recent = "\n".join(
        f"{'用户' if m.get('role') == 'user' else '助手'}："
        f"{_sanitize_dialogue(m.get('text', ''))[:300]}"
        for m in history[-6:]
    )
    try:
        raw = await chat(
            [
                {
                    "role": "system",
                    "content": (
                        "你在帮用户读论文。根据最近对话，把当前问题改写成不依赖上下文也能看懂的完整问题，"
                        "并给出用于检索的英文关键词（3-8 个）和中文关键词（3-8 个）。\n"
                        "只输出 JSON："
                        '{"standalone": "...", "en": ["..."], "zh": ["..."]}\n'
                        "当前问题本身已经完整时，standalone 原样返回；不要回答问题。"
                    ),
                },
                {"role": "user", "content": f"最近对话：\n{recent}\n\n当前问题：{question}"},
            ],
            temperature=0.1,
            max_tokens=600,
        )
    except Exception as e:
        log.info("追问改写失败：%s", e)
        return question, []

    data = _parse_json(raw)
    if not isinstance(data, dict):
        return question, []

    standalone = str(data.get("standalone") or "").strip()
    if not standalone or len(standalone) > 200:
        standalone = question

    words: list[str] = []
    for key in ("en", "zh"):
        for word in data.get(key) or []:
            if isinstance(word, str) and word.strip():
                words.append(word.strip())
    return standalone, words


def _parse_json(raw: str) -> dict | list | None:
    """解析模型返回的 JSON：容忍代码围栏，失败时抓第一个 {...} 块。"""
    text = re.sub(r"^`{3}(?:json)?\s*|\s*`{3}$", "", (raw or "").strip())
    try:
        return json.loads(text)
    except Exception:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except Exception:
            return None


def needs_external(question: str, top_score: float) -> bool:
    if _EXTERNAL_RE.search(question):
        return True
    return top_score < 1.2      # 论文里基本没匹配上


ANSWER_SYS = """你是论文助教。只根据给定的材料回答，用中文，简洁、分点、不要客套。

规则：
1. 材料里没有的信息不要编，直接说"论文中没有提到"；
2. 每一条论断后面必须标注来源，格式：
   [P3] 第 3 页  |  [R31] 参考文献 31  |  [W2] 网络结果 2
3. 如果材料之间矛盾，指出矛盾，不要强行统一；
4. 不要复述问题，直接给答案。"""

MEMORY_RULES = """
5. 【背景记忆】只用于调整讲法、承接前文、避免重复，不能作为论文事实、数字、结论的依据；
6. 概念标为"已懂"的不再展开；标为"困惑"的换一种讲法，先补前置；
7. 用户纠正过的以纠正为准；记忆与材料冲突时以材料为准；
8. 不要声称"我记得你……"，直接用调整后的讲法。"""


def build_context(
    hits: list[tuple[Doc, float]],
    refs: dict[int, str] | None = None,
    web: list[dict] | None = None,
    docs: list[Doc] | None = None,
) -> str:
    # "本文的主要贡献如下：" 这种引导句后面才是内容，命中时补上后一段
    order_map = {d.order: d for d in (docs or [])}
    parts: list[str] = []
    for d, _s in hits:
        body = d.translated or d.text
        nxt = order_map.get(d.order + 1)
        if nxt and nxt is not d:
            body = f"{body}\n{nxt.translated or nxt.text}"
        parts.append(f"[P{d.page}] {body[:900]}")
    for idx, title in (refs or {}).items():
        parts.append(f"[R{idx}] {title}")
    for i, w in enumerate(web or [], 1):
        parts.append(f"[W{i}] {w.get('title','')} — {w.get('content','')[:400]}")
    return "\n\n".join(parts)


def _sanitize_dialogue(text: object) -> str:
    """把历史对话压成单行：去控制字符，替换全角方括号，防止伪造块标题。"""
    value = str(text or "")
    value = _DIALOGUE_CONTROL_RE.sub("", value)
    value = value.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    value = re.sub(r"\s+", " ", value)
    return value.replace("【", "［").replace("】", "］").strip()


def _recent_block(recent: list[dict] | None) -> str:
    """最近几轮只留角色和正文，不带 client_id / sources 等内部字段。"""
    lines = [
        f"{'用户' if m.get('role') == 'user' else '助手'}："
        f"{_sanitize_dialogue(m.get('text', ''))[:300]}"
        for m in (recent or [])[-6:]
    ]
    return "【最近对话】\n" + "\n".join(lines)


def _answer_messages(
    question: str,
    context: str,
    memory: str = "",
    recent: list[dict] | None = None,
) -> list[dict]:
    system = ANSWER_SYS + (MEMORY_RULES if memory else "")
    user = f"【材料】\n{context}\n\n【问题】{question}"
    if recent:
        user = f"{_recent_block(recent)}\n\n{user}"
    if memory:
        user = f"【背景记忆】\n{memory}\n\n{user}"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


async def answer(
    question: str,
    context: str,
    memory: str = "",
    recent: list[dict] | None = None,
) -> str:
    raw = await chat(
        _answer_messages(question, context, memory, recent),
        temperature=0.2,
        max_tokens=4000,
    )
    return raw.strip()


async def answer_stream(
    question: str,
    context: str,
    memory: str = "",
    recent: list[dict] | None = None,
) -> AsyncIterator[str]:
    """流式回答：逐 delta 产出，调用方负责拼接与发送。"""
    async for piece in chat_stream(
        _answer_messages(question, context, memory, recent),
        temperature=0.2,
        max_tokens=4000,
    ):
        yield piece
