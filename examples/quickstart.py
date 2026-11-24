"""最小可运行示例：解析一篇 PDF 并翻译前 10 段。

    export PAPEREAD_API_KEY=sk-xxx
    python examples/quickstart.py path/to/paper.pdf

整篇翻译由桌面端调用外部 BabelDOC 引擎完成；这里只演示逐段翻译。
"""

import asyncio
import sys
from pathlib import Path

from paperead import build_glossary, parse_pdf, translate_block


async def main(pdf: str) -> None:
    paper = parse_pdf(pdf, figure_dir="output/assets")
    print(f"pages={paper.page_count} columns={paper.columns} blocks={len(paper.blocks)}")

    for block in paper.blocks[:8]:
        print(f"  [{block.order:03d}] {block.kind:10s} {block.text[:70]}")

    glossary = await build_glossary(paper.blocks)
    print(f"\n术语表 {len(glossary)} 条：{list(glossary.items())[:5]}")

    done = 0
    for block in paper.blocks:
        if not block.text.strip() or done >= 10:
            continue
        try:
            block.translated = await translate_block(block, glossary)
            done += 1
        except Exception as exc:  # noqa: BLE001 - 示例里只提示失败
            print(f"  翻译失败 [{block.order}]：{exc}")
    print(f"翻译 {done} 段")

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
