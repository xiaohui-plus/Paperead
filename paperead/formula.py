"""公式识别：把裁出来的公式图片转成 LaTeX。

流程：
    公式裁图（layout 已完成） → base64 → 视觉模型 → 清洗/校验 → Block.latex

设计约束：
- **只在解析阶段跑**，不在阅读时实时调用。阅读页永远秒开。
- **结果缓存**：按图片字节的 sha1 缓存，同一公式重跑零成本。
- **失败可回落**：识别不出来就保持空，前端自动显示原图，不影响阅读。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import re
from pathlib import Path

from .env import env, migrate_legacy_dir
from .layout import Block
from .llm import LLMConfig, OpenAICompatBackend, chat, set_backend

logger = logging.getLogger(__name__)

PROMPT = (
    "你是数学公式识别器。把图片中的公式转成 LaTeX。\n"
    "规则：\n"
    "1. 只输出 LaTeX 正文，不要 $ 或 $$ 包裹，不要 \\begin{equation}，"
    "不要 Markdown 代码块，不要任何解释；\n"
    "2. 多行/对齐公式用 \\begin{aligned} ... \\end{aligned}；\n"
    "3. 不要输出公式编号（如 (3)、(2.1)），编号由版式单独保存；\n"
    "4. 不要输出图表里的坐标轴刻度、图例文字；只识别真正的数学公式；\n"
    "5. 无法识别时只输出 NONE。"
)

_FENCE_RE = re.compile(r"^```(?:latex|tex)?\s*|\s*```$", re.IGNORECASE)


def _strip_wrappers(tex: str) -> str:
    tex = tex.strip()
    tex = _FENCE_RE.sub("", tex).strip()
    # 去掉 $...$ / \[...\] / \begin{equation}...\end{equation}
    for _ in range(3):
        before = tex
        tex = re.sub(r"^\s*\$\$?", "", tex)
        tex = re.sub(r"\$\$?\s*$", "", tex)
        tex = re.sub(r"^\s*\\\[", "", tex)
        tex = re.sub(r"\\\]\s*$", "", tex)
        tex = re.sub(r"^\s*\\begin\{(?:equation|displaymath|align)\*?\}", "", tex)
        tex = re.sub(r"\\end\{(?:equation|displaymath|align)\*?\}\s*$", "", tex)
        tex = tex.strip()
        if tex == before:
            break
    return tex


_NUMBER_SUFFIX_RE = re.compile(
    r"(?:\\tag\*?\{[^{}]*\}|[（(]\s*[A-Z]?\d{1,3}(?:\.\d+)?\s*[)）])\s*$"
)


def _strip_number(tex: str) -> str:
    """去掉模型擅自带上的编号（\\tag{3} 或行尾 (3)）。

    编号由版面解析单独保存到 `Block.eq_number`，前端右对齐渲染；
    留在 LaTeX 里会跟着公式居中，位置不对。
    """
    prev = None
    while tex and tex != prev:
        prev = tex
        tex = _NUMBER_SUFFIX_RE.sub("", tex).strip()
    return tex


def validate_latex(tex: str) -> tuple[bool, str]:
    """粗校验：括号配平、环境配对、长度合理。宁可判错回落到图片，也不要把坏公式交给 KaTeX。"""
    t = tex.strip()
    if not t or t.upper() == "NONE":
        return False, "empty"
    if len(t) > 2000:
        return False, "too long"
    if t.count("{") != t.count("}"):
        return False, "unbalanced braces"
    if t.count("\\begin") != t.count("\\end"):
        return False, "unbalanced environments"
    if t.count("\\left") != t.count("\\right"):
        return False, "unbalanced left/right"
    # 纯文本或中文解释（模型跑偏时会出现）
    letters = sum(1 for ch in t if ch.isascii() and ch.isalpha())
    if letters and letters / len(t) > 0.72 and "\\" not in t:
        return False, "looks like prose"
    return True, "ok"


def _cache_dir() -> Path:
    root = env("PAPEREAD_CACHE")
    if root:
        base = Path(root)
    else:
        cache = Path.home() / ".cache"
        base = migrate_legacy_dir(cache / "paperead", cache / "paper-reflow")
    d = base / "formula"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_key(image: bytes) -> str:
    return hashlib.sha1(image).hexdigest()


async def recognize_latex(image: bytes, model: str | None = None, backend=None) -> str:
    """单张公式图 → LaTeX（失败返回空串）。

    Args:
        backend: 可传入独立的 LLM 后端（服务端按请求传配置），默认用全局后端。
    """
    b64 = base64.b64encode(image).decode("ascii")
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}"},
                },
            ],
        }
    ]
    caller = backend.chat if backend is not None else chat
    raw = await caller(messages, model=model, temperature=0.0, max_tokens=1600)
    tex = _strip_wrappers(raw)
    tex = _strip_number(tex)
    ok, reason = validate_latex(tex)
    if not ok:
        logger.info("公式识别结果被丢弃（%s）：%s", reason, tex[:80])
        return ""
    return tex


async def recognize_equations(
    blocks: list[Block],
    concurrency: int = 4,
    force: bool = False,
    backend=None,
) -> dict:
    """给所有 equation 块补 `latex` 字段。

    Args:
        force: 忽略缓存重新识别。
        backend: 可传入独立的视觉模型后端；默认用全局后端。

    Returns:
        {"total": n, "ok": n, "failed": n, "cached": n}
    """
    targets = [
        b for b in blocks
        if b.kind == "equation" and b.image_path and (force or not b.latex)
    ]
    stats = {"total": len(targets), "ok": 0, "failed": 0, "cached": 0}
    if not targets:
        return stats

    cache = _cache_dir()
    sem = asyncio.Semaphore(concurrency)

    async def one(b: Block) -> None:
        path = Path(b.image_path or "")
        if not path.exists():
            stats["failed"] += 1
            return
        image = path.read_bytes()
        key = _cache_key(image)
        cpath = cache / f"{key}.tex"

        if not force and cpath.exists():
            b.latex = cpath.read_text(encoding="utf-8")
            stats["cached"] += 1
            return

        async with sem:
            for attempt in range(2):
                try:
                    tex = await recognize_latex(image, backend=backend)
                    if tex:
                        b.latex = tex
                        cpath.write_text(tex, encoding="utf-8")
                        stats["ok"] += 1
                        return
                except Exception as e:  # 单条失败不影响整篇
                    logger.warning("公式识别失败（第 %d 次）：%s", attempt + 1, e)
        stats["failed"] += 1

    await asyncio.gather(*(one(b) for b in targets))
    logger.info("公式识别完成：%s", stats)
    return stats


def configure(config: LLMConfig) -> None:
    """用一份显式配置创建后端（服务端按请求传配置时用）。"""
    set_backend(OpenAICompatBackend(config))
