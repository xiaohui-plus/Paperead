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
    r"领域内|发展趋势|和.*比|比.*好吗|202[4-9]|202[0-9]年|"
    r"相关研究|相关文献|别人怎么做|业界|开源实现|网上|谷歌|百度|搜一下",
    re.IGNORECASE,
)
# 概念/原理类问题：论文往往只给了本文的一个变体，读者要的是通用解释
_CONCEPT_RE = re.compile(
    r"是什么|什么是|为什么|为何|如何理解|怎么理解|原理|机制|含义|意思|定义|"
    r"区别|差异|不同点|优缺点|优劣|局限性|适用场景|应用场景|举例|例子|"
    r"背景|历史|由来|前置知识|入门|科普|怎么用|如何用|怎么实现|如何实现",
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


_FOLLOWUP_RE = re.compile(
    r"^(那|那么|所以|但是|还有|继续|再说|接着|然后|这个|那个|它|他|她|该|上述|前面|"
    r"刚才|上一条|上句|这里|这种|那样|这样|同样|为什么|怎么|如何|展开|解释一下)|"
    r"(它|这个|那个|该|上述|前面|刚才|上一条|上句|这里|这种|那样|这句话|这段|这部分)",
    re.IGNORECASE,
)


def looks_like_followup(question: str) -> bool:
    """问题是否依赖上下文（指代/省略/追问）。仅在这种情况下才需要模型改写。

    不看长度：短问题（「什么是注意力」「第二个问题」）往往本身完整，
    误判会让每轮都多一次模型调用，正是之前慢的来源之一。
    """
    q = (question or "").strip()
    if not q:
        return False
    return bool(_FOLLOWUP_RE.search(q))


def keywords(question: str, limit: int = 12) -> list[str]:
    """本地取检索关键词，替代每次提问都调模型扩充。"""
    seen: list[str] = []
    for token in tokenize(question or ""):
        if token in seen:
            continue
        seen.append(token)
        if len(seen) >= limit:
            break
    return seen


async def rewrite_and_expand(
    question: str, history: list[dict] | None = None
) -> tuple[str, list[str]]:
    """把追问改写成独立问题，并给出检索词。

    没有历史、或当前问题本身完整时直接返回本地关键词：不额外调用模型，
    论文材料由档案直供，关键词只用于本地定位与联网检索。
    """
    if not history or not looks_like_followup(question):
        return question, keywords(question)

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
    """是否需要联网。

    这里只判断「必须联网」的强信号；开关打开时调用方对每个问题都会做一次
    基础检索，本函数用于决定是否再扩大检索范围（补英文关键词/学术源）。
    """
    q = (question or "").strip()
    if not q:
        return False
    if _EXTERNAL_RE.search(q):
        return True
    if _CONCEPT_RE.search(q) and top_score < 3.5:
        return True
    return top_score < 1.2      # 论文里基本没匹配上


ANSWER_SYS = """你是论文助教。材料按可信度分层，回答以【用户引用】和【论文档案】为准，
【对话历史】只用于承接语境，【关于读者】只用于调整讲法。

材料标记：
- [P3 §3.2] 论文第 3 页第 3.2 节原文；标「全文」的段落是完整原文；
- 未标「全文」的段落只提供了首句（全篇覆盖用），不能把首句当成完整段落；
- [R31] 论文参考文献 31；[W2] 网络检索摘要（可能有错，谨慎采信）；
- [W2 正文] 论文材料不足时抓取到的网页正文，比搜索摘要完整，引用时给出来源链接。

回答规则：
1. 先直接回答问题本身，再补充依据；不要答非所问、不要复述问题。
2. 论文有依据的结论标 [P页码]，参考文献标 [R31]，网络标 [W2]；自己的领域常识标「（通识）」。
3. 绝对不要编造论文里没有的页码、数据、实验结论、方法名。材料只到首句时，
   要说明「论文该段只看到开头」，不要脑补中间内容。
4. 论文确实没写、或用户问的是论文之外的知识时，按 [W] 联网材料优先、其次领域常识回答，
   但要明确这不是论文原文；有 [W 正文] 时优先用它而不是只靠摘要。
5. 【用户引用】非空时，本次问题优先针对引用片段：先解释/回答引用的内容，
   再视需要补充上下文；引用里没有的内容不要硬塞。
6. 【对话历史】越近的轮次越完整、越远的越模糊；指代（「它」「这个方法」「上面说的」）
   按最近对话理解；早期对话只当背景，不能当作论文事实。
7. 【关于读者】用于决定讲法（深度、例子、是否补前置），不能当作论文事实或数据来源。
8. 材料不足时说明缺什么；材料之间冲突时指出差异，不要强行统一。
9. 中文，简洁、分点、不要客套。"""

MEMORY_RULES = """
补充：
- 【关于读者】里标为「已懂」的概念不再展开；标为「困惑」的换一种讲法、先补前置；
- 用户纠正过的以纠正为准；记忆与论文材料冲突时以论文材料为准；
- 不要声称「我记得你……」，直接使用调整后的讲法。"""

WEB_NOTE = """
【联网状态】本次未能获取到有效的网络结果。若论文材料不足，请基于领域常识回答，
并在相应结论后标注「（通识）」；明确告知用户这些内容不是论文原文。"""


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
        title = w.get("title", "")
        url = w.get("url", "")
        body = (w.get("content") or "")[:600]
        head = f"{title}（{url}）" if url else title
        parts.append(f"[W{i}] {head} — {body}")
    return "\n\n".join(parts)


def _sanitize_dialogue(text: object) -> str:
    """把历史对话压成单行：去控制字符，替换全角方括号，防止伪造块标题。"""
    value = str(text or "")
    value = _DIALOGUE_CONTROL_RE.sub("", value)
    value = value.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    value = re.sub(r"\s+", " ", value)
    return value.replace("【", "［").replace("】", "］").strip()


def _recent_block(recent: list[dict] | None) -> str:
    """把历史按「记忆曲线」压成材料：越近记得越完整，越远越模糊。"""
    return history_curve(recent)


HISTORY_TOTAL_LIMIT = 3_600
# (轮次上限, 问题上限, 答案上限)
_HISTORY_LEVELS = (
    (2, 600, 700),
    (5, 320, 320),
    (10, 160, 120),
)


def history_curve(recent: list[dict] | None) -> str:
    """越近的轮次保留越多细节，越远的只留问题要点。

    这不是检索：只是把已有对话按距离做确定性压缩，不额外调用模型。
    """
    messages = list(recent or [])
    if not messages:
        return ""

    turns: list[tuple[str, str]] = []
    pending_q = ""
    for msg in messages:
        role = msg.get("role")
        text = _sanitize_dialogue(msg.get("text", ""))
        if role == "user":
            if pending_q:
                turns.append((pending_q, ""))
            pending_q = text
        elif pending_q:
            turns.append((pending_q, text))
            pending_q = ""
    if pending_q:
        turns.append((pending_q, ""))

    if not turns:
        return ""

    lines: list[str] = ["【历史对话·越近越完整】"]
    budget = HISTORY_TOTAL_LIMIT
    for distance, (question, answer) in enumerate(reversed(turns), 1):
        for until, q_limit, a_limit in _HISTORY_LEVELS:
            if distance <= until:
                break
        else:
            q_limit, a_limit = 90, 0

        if budget <= 0:
            lines.append("- …（更早的对话已省略）")
            break

        q = _clip_text(question, q_limit)
        if q_limit < 160:
            q = q.rstrip("…") + "…" if len(question) > q_limit else q
        note = "（较早，细节可能已模糊）" if distance > 3 else ""
        line = f"- 问：{q}"
        if answer and a_limit > 0:
            line += f"\n  答：{_clip_text(answer, a_limit)}"
        if note:
            line += f"\n  {note}"
        if len(line) > budget:
            line = _clip_text(line, max(0, budget))
        lines.append(line)
        budget -= len(line)
    lines.append("（最近一轮最可信；更早的内容只作背景，不作为论文事实。）")
    return "\n".join(lines)


def _clip_text(text: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", text or "").strip()
    if limit <= 0:
        return ""
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def _answer_messages(
    question: str,
    context: str,
    memory: str = "",
    recent: list[dict] | None = None,
    web_empty: bool = False,
) -> list[dict]:
    system = ANSWER_SYS + (MEMORY_RULES if memory else "")
    parts: list[str] = []
    history = history_curve(recent)
    if history:
        parts.append(history)
    parts.append(
        "【关于读者】\n"
        + (memory or "读者是正在精读这篇论文的研究者；未提供更多个人背景，不要臆测其领域与目标。")
    )
    parts.append(f"【材料】\n{context}")
    parts.append(f"【本次问题】{question}")
    user = "\n\n".join(parts)
    if web_empty:
        user = f"{user}\n{WEB_NOTE}"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


async def answer(
    question: str,
    context: str,
    memory: str = "",
    recent: list[dict] | None = None,
    web_empty: bool = False,
) -> str:
    raw = await chat(
        _answer_messages(question, context, memory, recent, web_empty),
        temperature=0.2,
        max_tokens=4000,
    )
    return raw.strip()


async def answer_stream(
    question: str,
    context: str,
    memory: str = "",
    recent: list[dict] | None = None,
    web_empty: bool = False,
) -> AsyncIterator[str]:
    """流式回答：逐 delta 产出，调用方负责拼接与发送。"""
    async for piece in chat_stream(
        _answer_messages(question, context, memory, recent, web_empty),
        temperature=0.2,
        max_tokens=4000,
    ):
        yield piece
