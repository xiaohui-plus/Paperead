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

当前定位是 **英译多语**：术语表、上下文衔接和占位符保护与语种无关；
质检的字符长度区间按目标语言调整。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Awaitable, Callable, Iterable

from .layout import Block
from .llm import active_model, chat as llm_chat
from .textnorm import (
    has_dot_leader,
    is_dot_leader_only,
    strip_dot_leaders,
    strip_unrenderable,
)

logger = logging.getLogger(__name__)


_THINKING_PREFIXES = (
    "deepseek",
    "qwen3",
    "qwen-3",
    "qwq",
    "kimi-k3",
    "kimi-k2.5",
    "o1",
    "o3",
    "o4",
    "glm-z1",
    "doubao-1.5-thinking",
    "hunyuan-t1",
)


def _task_extra_body(model: str | None) -> dict | None:
    """推理模型默认开思考；翻译/术语表关掉，避免 reasoning 吃光输出预算。

    不同网关的开关字段不完全一致：这里先按 ``thinking`` 发送，调用失败且
    错误信息提到 thinking 时 ``_translate_chat`` 会去掉该字段再试一次。
    """
    name = (model or active_model() or "").lower()
    if name.startswith(_THINKING_PREFIXES):
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
               r"\s*\(?(?:\d+(?:\.\d+)*|[IVXLCDM]+|[A-Z]\d+)[A-Za-z]?\)?", re.I),
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

def _glossary_sys(target_name: str) -> str:
    return (
        "你是学术论文翻译专家。请从给定的论文片段中提取领域术语，"
        f"给出规范的{target_name}译名。"
        '只输出 JSON，形如 {"terms": [{"en": "GWAS", "target": "全基因组关联分析"}]}。'
        "只提取真正需要统一的专业术语（10-40 个），不要包含普通词汇、作者名、机构名。"
    )


async def build_glossary(
    blocks: Iterable[Block],
    model: str | None = None,
    max_chars: int = 6000,
    target_name: str = "中文",
) -> dict[str, str]:
    """扫描标题/摘要/章节标题，生成 英文→目标语言 术语表。失败时返回空表，不影响主流程。"""
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
                    {"role": "system", "content": _glossary_sys(target_name)},
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
                    en = (t.get("en") or "").strip()
                    # 兼容旧缓存里的 zh 字段
                    target = (t.get("target") or t.get("zh") or "").strip()
                    if en and target:
                        out[en] = target
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

def _system_prompt(target_name: str) -> str:
    return (
        f"你是学术论文翻译专家，负责把英文学术论文翻译成{target_name}。要求：\n"
        "1. 只输出译文正文，不要任何解释、前言、引号或 Markdown 代码块；\n"
        "2. 学术书面语，忠实、准确、通顺，不增删内容，不概括；\n"
        "3. 保持原文的段落、编号、列表结构；目录条目只译标题文字，"
        "不要输出点线引导符（如 . . . . .）或目录页码；\n"
        "4. 占位符 ⟦P0⟧ ⟦P1⟧ 必须原样保留，不要翻译、不要改动；\n"
        "5. 严格遵循给定术语表；术语表未覆盖的专有名词保留英文原名。"
    )


def _build_prompt(
    text: str,
    glossary: dict[str, str],
    section: str,
    prev_tail: str,
    next_head: str,
    target_name: str,
) -> str:
    parts: list[str] = []
    if glossary:
        items = "；".join(f"{en} → {tgt}" for en, tgt in list(glossary.items())[:60])
        parts.append(f"【术语表】{items}")
    if section:
        parts.append(f"【当前章节】{section}")
    if prev_tail:
        parts.append(f"【上一段译文结尾（仅供衔接，不要重复翻译）】{prev_tail}")
    if next_head:
        parts.append(f"【下一段原文开头（供语境参考）】{next_head}")
    parts.append(f"【待翻译段落】\n{text}")
    parts.append(f"请只输出这一段的{target_name}译文。")
    return "\n\n".join(parts)


async def translate_block(
    block: Block,
    glossary: dict[str, str] | None = None,
    prev_tail: str = "",
    next_head: str = "",
    model: str | None = None,
    temperature: float = 0.25,
    target_name: str = "中文",
) -> str:
    """翻译单个块。返回译文（失败抛异常，由上层重试或降级）。"""
    # 历史论文的 blocks.json 里仍留着目录点线：先去掉再送模型，
    # 否则提示词再约束也没用——模型看到什么就照抄什么。
    source = strip_dot_leaders(block.text) or block.text
    masked, pieces = protect(source)
    prompt = _build_prompt(
        masked, glossary or {}, block.section, prev_tail, next_head, target_name
    )
    # 推理模型的 reasoning token 计入 max_tokens，给小了会被截断成半句
    budget = min(8000, max(2048, int(len(masked) * 1.2) + 1500))
    out = await _translate_chat(
        messages=[
            {"role": "system", "content": _system_prompt(target_name)},
            {"role": "user", "content": prompt},
        ],
        model=model,
        temperature=temperature,
        max_tokens=budget,
    )
    out = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", out.strip()).strip()
    if not out:
        raise RuntimeError("模型返回空译文")
    return strip_unrenderable(restore(strip_dot_leaders(out), pieces))


async def translate_text(
    text: str,
    target_name: str = "中文",
    model: str | None = None,
    temperature: float = 0.2,
) -> str:
    """翻译划选的一小段文字（句子 / 短语 / 半段）。

    和整篇段落翻译共用系统提示与占位符保护，引用编号、公式、URL 等会原样保留；
    但不做术语表与上下文锚定，也不写回论文，适合阅读时的即时划词翻译。
    """
    source = strip_dot_leaders(text).strip()
    if not source:
        raise ValueError("没有可翻译的文字")
    masked, pieces = protect(source)
    prompt = (
        f"【待翻译片段】\n{masked}\n\n"
        f"请只输出这段文字的{target_name}译文；若原文是术语、标题或短语，"
        "保持简洁，不要扩写，也不要补充解释。"
    )
    # 划词片段通常很短，给 512 起步即可；推理模型仍留足预算
    budget = min(4096, max(512, int(len(masked) * 1.6) + 320))
    out = await _translate_chat(
        messages=[
            {"role": "system", "content": _system_prompt(target_name)},
            {"role": "user", "content": prompt},
        ],
        model=model,
        temperature=temperature,
        max_tokens=budget,
    )
    out = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", out.strip()).strip()
    if not out:
        raise RuntimeError("模型返回空译文")
    return strip_unrenderable(restore(strip_dot_leaders(out), pieces))


def quality_ok(
    source: str,
    target: str,
    pieces: int = 0,
    lang: str = "zh-Hans",
) -> bool:
    """粗粒度质检：译文长度合理、占位符数量正确。

    不同目标语言的字符长度比例差异很大：中文最紧凑，日韩居中，
    英法德西俄接近原文长度。区间放宽只为拦住明显的漏译和复读。
    """
    if not target.strip():
        return False
    src_len = max(len(source), 1)
    ratio = len(target) / src_len
    if lang in ("zh-Hans", "zh-Hant"):
        low, high = 0.1, 0.9
    elif lang in ("ja", "ko"):
        low, high = 0.18, 1.6
    else:
        low, high = 0.3, 2.4
    if not low <= ratio <= high:
        return False
    # 译文是「已还原」的文本，占位符 ⟦P0⟧ 已经换回原文片段；这里直接检查
    # 每个受保护片段是否还在译文里。以前比对占位符数量时译文永远不含 ⟦P0⟧，
    # 校验等于没做，模型吞掉公式/引用编号也不会被发现。
    _masked, pieces = protect(source)
    if pieces:
        compact_target = re.sub(r"\s+", "", target)
        for piece in pieces:
            needle = re.sub(r"\s+", "", piece)
            if needle and needle not in compact_target:
                return False
    return True


def _is_translatable(text: str) -> bool:
    """过滤残留的公式碎片与目录点线：没有足够正文内容的块不送翻译。

    版面解析后仍可能有零散的公式片段（如 `1 + min(dx, dy) + (d/2)[(d/2)`）被
    当成段落，送给模型只会得到胡言乱语；目录页的引导符点线块同理——模型只会
    把点线原样复制一遍。判据在剥掉点线之后再看字母占比，避免把真正的目录
    标题误杀。
    """
    cleaned = strip_dot_leaders(text)
    if not cleaned.strip() or is_dot_leader_only(cleaned):
        return False
    letters = sum(1 for ch in cleaned if ch.isascii() and ch.isalpha())
    return letters / max(len(cleaned), 1) > 0.5
