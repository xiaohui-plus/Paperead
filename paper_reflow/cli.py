"""命令行入口。

    # 只解析，导出结构化块
    paper-reflow paper.pdf -o out

    # 解析 + 全文翻译
    paper-reflow paper.pdf -o out --translate

    # 只翻前 20 段，先看效果
    paper-reflow paper.pdf -o out --translate --limit 20

也可以直接用模块方式跑：python -m paper_reflow.cli paper.pdf --translate
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .formula import recognize_equations
from .layout import parse_pdf
from .translate import build_glossary, translate_paper


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="paper-reflow",
        description="学术论文 PDF 版面重排 + 英中逐段翻译",
    )
    p.add_argument("pdf", help="PDF 路径")
    p.add_argument("-o", "--out", default="output", help="输出目录（默认 output/）")
    p.add_argument("--translate", action="store_true", help="解析后逐段翻译")
    p.add_argument("--formula", action="store_true", help="识别显示公式为 LaTeX（需要视觉模型）")
    p.add_argument("--limit", type=int, default=None, help="最多翻译多少段（调试用）")
    p.add_argument("--concurrency", type=int, default=4, help="并发数（默认 4）")
    p.add_argument("--no-figures", action="store_true", help="不抽取图片/表格/公式")
    p.add_argument("--json", metavar="PATH", default=None, help="块数据输出路径")
    return p


async def _translate(paper, args) -> None:
    glossary = await build_glossary(paper.blocks)
    print(f"术语表：{len(glossary)} 条")
    stats = await translate_paper(
        paper.blocks,
        glossary,
        concurrency=args.concurrency,
        limit=args.limit,
    )
    print(f"翻译完成：{stats}")


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    src = Path(args.pdf)
    if not src.exists():
        print(f"文件不存在：{src}", file=sys.stderr)
        return 2

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    paper = parse_pdf(
        src,
        paper_id=src.stem,
        figure_dir=out / "assets",
        with_figures=not args.no_figures,
    )
    print(
        f"解析完成：{paper.page_count} 页 / {paper.columns} 栏 / "
        f"{len(paper.blocks)} 块（正文字号 {paper.body_size}pt）"
    )

    if args.formula:
        stats = asyncio.run(recognize_equations(paper.blocks, concurrency=args.concurrency))
        print(f"公式识别：{stats}")

    if args.translate:
        asyncio.run(_translate(paper, args))

    payload = {
        "source": str(src),
        "page_count": paper.page_count,
        "columns": paper.columns,
        "blocks": [b.to_dict() for b in paper.blocks],
    }
    json_path = Path(args.json) if args.json else out / f"{src.stem}.blocks.json"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"已写出：{json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
