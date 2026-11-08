"""论文段落翻译：术语表一致 + 占位符保护 + 上下文锚定。

为什么不能「每段独立丢给模型」
------------------------------
学术论文里同一术语在不同段落往往被译成不同说法（GWAS → 「全基因组关联分析」/
「基因组关联研究」），指代也会断。做法是两阶段：

1. **Pass A 术语表**：用标题 + 摘要 + 各级标题做一次轻量扫描，产出 EN→ZH 术语表；
2. **Pass B 逐段翻译**：每段请求都带上术语表 + 所在章节 + 上一段译文结尾 +
   下一段原文开头，让模型在局部上下文中保持连贯。

**占位符保护**：公式、引用编号、图表编号、URL、基因名/物种名先替换成 `⟦P0⟧`
一类 token，翻译完再还原。不做这一步，`$…$` 和 `[12]` 会被模型改写或吞掉。

当前定位是 **英译中**；术语表提示词与译文质检阈值都按中英字符比设计。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Awaitable, Callable, Iterable

from .layout import Block
from .llm import active_model, chat as llm_chat

logger = logging.getLogger(__name__)


def _task_extra_body(model: str | None) -> dict | None:
    """DeepSeek V4.1 默认开思考；翻译/术语表关掉思考，避免 reasoning 吃光 token。"""
    name = (model or active_model() or "").lower()
    if name.startswith("deepseek"):
        return {"thinking": {"type": "disabled"}}
    return None


async def _translate_chat(**kwargs) -> str:
    extra = _task_extra_body(kwargs.get("model"))
    try:
        return await llm_chat(extra_body=extra, **kwargs)
    except Exception as e:
        # 某些中转/兼容实现不认识 thinking 参数：去掉后再试一次
        if extra and "thinking" in str(e).lower():
            return await llm_chat(extra_body=None, **kwargs)
        raise

# ── 占位符保护 ─────────────────────────────────────────────────────────

_PROTECT_PATTERNS: list[re.Pattern] = [
    re.compile(r"\$\$?.+?\$\$?", re.S),                      # $...$ / $$...$$
    re.compile(r"\\\((?:.|\n)*?\\\)"),                        # \( ... \)
    re.compile(r"\\\[(?:.|\n)*?\\\]"),                        # \[ ... \]
    re.compile(r"\\(?:cite|ref|eqref|autoref|label)\{[^}]*\}"),
    re.compile(r"\\[a-zA-Z]+\*?(?:\{[^{}]*\}){0,2}"),         # 其它 LaTeX 命令
    re.compile(r"\[\s*\d+(?:\s*[-,–]\s*\d+)*\s*\]"),          # [12] / [3-5] / [12,13]
    re.compile(                                                # (Smith et al., 2020)
        r"\((?:[A-Z][A-Za-z\-']+(?:\s+(?:et\s+al\.?|and|&)\s*[A-Za-z\-']*)?"
        r",?\s*\d{4}[a-z]?)\)"
    ),
    re.compile(r"\b(?:Fig(?:ure|s)?\.?|Tab(?:le|s)?\.?|Eq(?:uation|s)?\.?|Sec(?:tion|s)?\.?)"
               r"\s*\(?\d+[A-Za-z]?\)?"),
    re.compile(r"https?://\S+|doi:\s*\S+", re.I),
    re.compile(r"\b[A-Z]{2,3}\d{3,}\b"),                       # AT1G01010 类基因号
    re.compile(r"\b[A-Z][a-z]{1,3}[A-Z][A-Za-z0-9]{2,}\b"),    # OsDREB1A / GraphSAGE
]

_PH_RE = re.compile(r"⟦\s*P?(\d+)\s*⟧")


def protect(text: str) -> tuple[str, list[str]]:
    """把需要原样保留的片段替换为占位符，返回 (遮蔽文本, 片段列表)。

    占位符特意用 `⟦P0⟧` 而不是 `⟦0⟧`：模型极不可能自己产出这种形态，避免
    restore 时把译文里正常的 `[2]` 误当成占位符、导致引用编号整体错位。
    """
    pieces: list[str] = []

    def repl(m: re.Match) -> str:
        pieces.append(m.group(0))
        return f"⟦P{len(pieces) - 1}⟧"

    out = text
    for pat in _PROTECT_PATTERNS:
        out = pat.sub(repl, out)
    return out, pieces


def restore(text: str, pieces: list[str]) -> str:
    """还原占位符。只认 `⟦P0⟧` 形态，不做数字方括号的兜底猜测。"""
    def repl(m: re.Match) -> str:
        idx = int(m.group(1))
        return pieces[idx] if 0 <= idx < len(pieces) else m.group(0)

    return _PH_RE.sub(repl, text)


# ── 术语表 ─────────────────────────────────────────────────────────────

_GLOSSARY_SYS = (
    "你是学术论文翻译专家。请从给定的论文片段中提取领域术语，给出规范中文译名。"
    "只输出 JSON，形如 {\"terms\": [{\"en\": \"GWAS\", \"zh\": \"全基因组关联分析\"}]}。"
    "只提取真正需要统一的专业术语（10-40 个），不要包含普通词汇、作者名、机构名。"
)


async def build_glossary(
    blocks: Iterable[Block],
    model: str | None = None,
    max_chars: int = 6000,
) -> dict[str, str]:
    """扫描标题/摘要/章节标题，生成 EN→ZH 术语表。失败时返回空表，不影响主流程。"""
    sample: list[str] = []
    total = 0
    for b in blocks:
        if b.kind in ("title", "abstract", "caption") or b.kind == "heading":
            if not b.text:
                continue
            sample.append(b.text)
            total += len(b.text)
            if total > max_chars:
                break
    # 摘要通常是最长的块，单独补一段正文样本
    for b in blocks:
        if b.kind == "para" and b.text and total <= max_chars:
            sample.append(b.text[:1500])
            total += 1500
            break

    payload = "\n".join(sample)[:max_chars]
    if not payload.strip():
        return {}

    # 兜底模型可能是推理模型，reasoning token 也计入 max_tokens，
    # 给小了会出现 finish_reason=length 且 content 为空
    for budget in (4096, 8192):
        try:
            content = await _translate_chat(
                messages=[
                    {"role": "system", "content": _GLOSSARY_SYS},
                    {"role": "user", "content": payload},
                ],
                model=model,
                temperature=0.1,
                max_tokens=budget,
            )
            if not content.strip():
                continue
            data = _parse_json(content)
            terms = data.get("terms") or []
            out: dict[str, str] = {}
            for t in terms:
                if isinstance(t, dict):
                    en, zh = (t.get("en") or "").strip(), (t.get("zh") or "").strip()
                    if en and zh:
                        out[en] = zh
            logger.info("术语表构建完成：%d 条", len(out))
            return out
        except Exception as e:
            logger.warning("术语表构建失败（max_tokens=%d）：%s", budget, e)
    return {}


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return {}


# ── 段翻译 ─────────────────────────────────────────────────────────────

_SYS = (
    "你是学术论文翻译专家，负责把英文学术论文翻译成中文。要求：\n"
    "1. 只输出译文正文，不要任何解释、前言、引号或 Markdown 代码块；\n"
    "2. 学术书面语，忠实、准确、通顺，不增删内容，不概括；\n"
    "3. 保持原文的段落、编号、列表结构；\n"
    "4. 占位符 ⟦P0⟧ ⟦P1⟧ 必须原样保留，不要翻译、不要改动；\n"
    "5. 严格遵循给定术语表；术语表未覆盖的专有名词保留英文。"
)


def _build_prompt(
    text: str,
    glossary: dict[str, str],
    section: str,
    prev_tail: str,
    next_head: str,
) -> str:
    parts: list[str] = []
    if glossary:
        items = "；".join(f"{en} → {zh}" for en, zh in list(glossary.items())[:60])
        parts.append(f"【术语表】{items}")
    if section:
        parts.append(f"【当前章节】{section}")
    if prev_tail:
        parts.append(f"【上一段译文结尾（仅供衔接，不要重复翻译）】{prev_tail}")
    if next_head:
        parts.append(f"【下一段原文开头（供语境参考）】{next_head}")
    parts.append(f"【待翻译段落】\n{text}")
    parts.append("请只输出这一段的中文译文。")
    return "\n\n".join(parts)


async def translate_block(
    block: Block,
    glossary: dict[str, str] | None = None,
    prev_tail: str = "",
    next_head: str = "",
    model: str | None = None,
    temperature: float = 0.25,
) -> str:
    """翻译单个块。返回译文（失败抛异常，由上层重试或降级）。"""
    masked, pieces = protect(block.text)
    prompt = _build_prompt(masked, glossary or {}, block.section, prev_tail, next_head)
    # 推理模型的 reasoning token 计入 max_tokens，给小了会被截断成半句
    budget = min(8000, max(2048, int(len(masked) * 1.2) + 1500))
    out = await _translate_chat(
        messages=[
            {"role": "system", "content": _SYS},
            {"role": "user", "content": prompt},
        ],
        model=model,
        temperature=temperature,
        max_tokens=budget,
    )
    out = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", out.strip()).strip()
    if not out:
        raise RuntimeError("模型返回空译文")
    return restore(out, pieces)


def quality_ok(source: str, target: str, pieces: int = 0) -> bool:
    """粗粒度质检：译文长度合理、占位符数量正确。

    英译中的字符数比通常在 0.15~0.6（中文更紧凑），超出区间多半是漏译或复读。
    """
    if not target.strip():
        return False
    src_len = max(len(source), 1)
    ratio = len(target) / src_len
    if not 0.15 <= ratio <= 1.5:
        return False
    expected = len(_PH_RE.findall(protect(source)[0]))
    got = len(_PH_RE.findall(target))
    if got and got != expected:
        return False
    return True


def _is_translatable(text: str) -> bool:
    """过滤残留的公式碎片：英文字母占比过低的块不送翻译。

    版面解析后仍可能有零散的公式片段（如 `1 + min(dx, dy) + (d/2)[(d/2)`）被
    当成段落，送给模型只会得到胡言乱语。
    """
    letters = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    return letters / max(len(text), 1) > 0.5


async def translate_paper(
    blocks: list[Block],
    glossary: dict[str, str] | None = None,
    model: str | None = None,
    concurrency: int = 4,
    on_block: Callable[[Block], Awaitable[None] | None] | None = None,
    limit: int | None = None,
) -> dict:
    """逐段翻译整篇论文，原地写入 block.translated。

    Args:
        on_block: 每完成一段的回调（可用于 SSE 推送 / 写库）。

    Returns:
        {"translated": n, "failed": n, "quality_warn": n}
    """
    glossary = glossary or {}
    targets = [
        b for b in blocks
        if b.translatable and b.text.strip() and _is_translatable(b.text)
    ]
    if limit:
        targets = targets[:limit]

    sem = asyncio.Semaphore(concurrency)
    stats = {"translated": 0, "failed": 0, "quality_warn": 0}

    async def run(i: int, b: Block) -> None:
        prev_tail = targets[i - 1].translated[-120:] if i > 0 else ""
        next_head = targets[i + 1].text[:120] if i + 1 < len(targets) else ""
        async with sem:
            for attempt in range(2):
                try:
                    out = await translate_block(
                        b, glossary, prev_tail, next_head, model=model,
                        temperature=0.25 if attempt == 0 else 0.15,
                    )
                    ok = quality_ok(b.text, out)
                    if ok or attempt == 1:
                        b.translated = out
                        stats["translated"] += 1
                        if not ok:
                            stats["quality_warn"] += 1
                        break
                except Exception as e:
                    logger.warning("段落翻译失败（第 %d 次）：%s", attempt + 1, e)
                    if attempt == 1:
                        stats["failed"] += 1
        if on_block:
            r = on_block(b)
            if asyncio.iscoroutine(r):
                await r

    await asyncio.gather(*(run(i, b) for i, b in enumerate(targets)))
    logger.info("翻译完成：%s", stats)
    return stats
