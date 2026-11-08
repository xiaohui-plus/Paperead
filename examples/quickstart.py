"""最小可运行示例：解析一篇 PDF 并翻译前 10 段。

    export PAPEREAD_API_KEY=sk-xxx
    python examples/quickstart.py path/to/paper.pdf
"""

import asyncio
import sys
from pathlib import Path

from paperead import build_glossary, parse_pdf, translate_paper


async def main(pdf: str) -> None:
    paper = parse_pdf(pdf, figure_dir="output/assets")
    print(f"pages={paper.page_count} columns={paper.columns} blocks={len(paper.blocks)}")

    for block in paper.blocks[:8]:
        print(f"  [{block.order:03d}] {block.kind:10s} {block.text[:70]}")

    glossary = await build_glossary(paper.blocks)
    print(f"\n术语表 {len(glossary)} 条：{list(glossary.items())[:5]}")

    stats = await translate_paper(paper.blocks, glossary, limit=10)
    print(f"翻译：{stats}")

    for block in paper.blocks:
        if block.translated:
            print(f"\n[{block.order}] {block.kind}")
            print(f"  EN: {block.text[:120]}")
            print(f"  ZH: {block.translated[:120]}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    asyncio.run(main(sys.argv[1]))
