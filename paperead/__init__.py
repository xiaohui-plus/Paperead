"""Paperead：学术论文 PDF 版面重排 + 英中逐段翻译。

    from paperead import parse_pdf, build_glossary, translate_paper

    paper = parse_pdf("paper.pdf", figure_dir="assets")
    glossary = await build_glossary(paper.blocks)
    await translate_paper(paper.blocks, glossary)

    for block in paper.blocks:
        print(block.kind, block.text[:60], "->", block.translated[:60])
"""

from .formula import recognize_equations, recognize_latex, validate_latex
from .layout import Block, ParsedPaper, parse_pdf
from .llm import (
    LLMBackend,
    LLMConfig,
    OpenAICompatBackend,
    get_backend,
    set_backend,
)
from .translate import (
    build_glossary,
    protect,
    quality_ok,
    restore,
    translate_block,
    translate_paper,
)

__version__ = "0.2.6"

__all__ = [
    # 解析
    "Block",
    "ParsedPaper",
    "parse_pdf",
    # 公式
    "recognize_latex",
    "recognize_equations",
    "validate_latex",
    # 翻译
    "build_glossary",
    "translate_paper",
    "translate_block",
    "protect",
    "restore",
    "quality_ok",
    # LLM 接入
    "LLMBackend",
    "LLMConfig",
    "OpenAICompatBackend",
    "set_backend",
    "get_backend",
    "__version__",
]
