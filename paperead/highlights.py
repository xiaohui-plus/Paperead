"""自动阅读重点：从已翻译的段落里抽取研究目标 / 方法 / 结论。

只在用户已经看到（已经翻译过）的页上运行，由翻译流程顺带触发；
抽取结果同时带原文和译文片段，前端用不同颜色画在正文里，
并在「阅读重点」面板按类别摘录。

严格校验片段：模型必须逐字复制原文和译文，找不到就丢弃，
不靠模糊匹配，避免把改写过的句子标进正文。
"""

from __future__ import annotations

import json
import logging
import re

from .layout import Block
from .translate import _parse_json, _translate_chat

logger = logging.getLogger(__name__)

CATEGORIES = {
    "objective": "研究目标",
    "method": "研究方法",
    "conclusion": "主要结论",
}
# 一页最多标这么多条，避免整页都是荧光笔
MAX_PER_PAGE = 8
MAX_QUOTE = 500

_SYS = (
    "你是论文阅读助手，负责从已经翻译好的论文段落里找出三类信息：\n"
    "1. objective（研究目标）：作者要解决的问题、提出的假设、本文目标；\n"
    "2. method（研究方法）：数据、模型、实验设计、训练或评价方式；\n"
    "3. conclusion（主要结论）：主要发现、效果、局限或启示。\n"
    "只挑明确写了这些内容的句子；没有就留空，不要推断，不要把背景介绍当结论。\n"
    "引用必须是原文和译文中逐字连续的片段，不要改写、不要补字、不要省略号；\n"
    f"每条不超过 {MAX_QUOTE} 个字符；每个段落每类最多 1 条；一页最多 {MAX_PER_PAGE} 条。\n"
    "只输出 JSON，不要解释，格式："
    '{"highlights":[{"block_id":"b0002","category":"objective",'
    '"quote_en":"...","quote_zh":"..."}]}'
)


def _payload(blocks: list[Block]) -> list[dict]:
    items = []
    total = 0
    for block in blocks:
        if block.kind != "para":
            continue
        en = (block.text or "").strip()
        zh = (block.translated or "").strip()
        if not en or not zh:
            continue
        # 一页最多送 12 段，单段截断，避免超出上下文
        if len(items) >= 12:
            break
        item = {"block_id": block.id, "en": en[:3000], "zh": zh[:3000]}
        size = len(item["en"]) + len(item["zh"])
        if total + size > 12000:
            break
        total += size
        items.append(item)
    return items


def _extract_offsets(block: Block, item: dict) -> dict | None:
    category = str(item.get("category") or "").strip().lower()
    if category not in CATEGORIES:
        return None
    quote_en = str(item.get("quote_en") or "").strip().strip('"“”')
    quote_zh = str(item.get("quote_zh") or "").strip().strip('"“”')
    if not quote_en or not quote_zh:
        return None
    if len(quote_en) > MAX_QUOTE or len(quote_zh) > MAX_QUOTE:
        return None

    start = block.text.find(quote_en)
    zh_start = (block.translated or "").find(quote_zh)
    if start < 0 or zh_start < 0:
        return None
    return {
        "block_id": block.id,
        "category": category,
        "quote_en": quote_en,
        "quote_zh": quote_zh,
        "start": start,
        "end": start + len(quote_en),
        "zh_start": zh_start,
        "zh_end": zh_start + len(quote_zh),
    }


async def extract_highlights(
    blocks: list[Block],
    model: str | None = None,
) -> list[dict]:
    """对一页的段落做一次批量抽取，返回可直接落盘的条目。"""
    items = _payload(blocks)
    if not items:
        return []

    payload = (
        "【待分析段落】\n"
        + json.dumps(items, ensure_ascii=False, indent=1)
        + "\n\n请找出研究目标、研究方法、主要结论，只输出 JSON。"
    )
    try:
        content = await _translate_chat(
            messages=[
                {"role": "system", "content": _SYS},
                {"role": "user", "content": payload},
            ],
            model=model,
            temperature=0.1,
            max_tokens=4000,
        )
    except Exception as exc:
        # 交给上层决定是否重试：网络/限流失败不应该被当成「这页没有重点」
        logger.warning("阅读重点抽取失败：%s", exc)
        raise

    data = _parse_json(content)
    raw = data.get("highlights") or []
    if not isinstance(raw, list):
        return []

    by_id = {block.id: block for block in blocks}
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        block = by_id.get(str(item.get("block_id") or ""))
        if block is None:
            continue
        parsed = _extract_offsets(block, item)
        if parsed is None:
            continue
        key = (parsed["block_id"], parsed["category"])
        if key in seen:
            continue
        seen.add(key)
        out.append(parsed)
        if len(out) >= MAX_PER_PAGE:
            break
    return out
