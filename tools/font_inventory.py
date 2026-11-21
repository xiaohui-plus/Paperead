"""字体覆盖率扫描：发现未注册的数学字体。

用法：
    python tools/font_inventory.py <pdf 或目录> [--json out.json]
    python tools/font_inventory.py <pdf> --unregistered-only

输出每个字体名被解析成的角色（anchor/aux/text/cjk/unknown）、字符量与
数学字符量。解析器新增字体族后，用它确认「哪一族还没被注册、多少字符
没有解码」，而不是等用户看到方块再回头补白名单。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paperead.fonts.registry import FontRole, resolve_font  # noqa: E402


def _is_math_codepoint(ch: str) -> bool:
    code = ord(ch)
    return (
        0xE000 <= code <= 0xF8FF
        or 0x1D400 <= code <= 0x1D7FF
        or 0x0370 <= code <= 0x03FF
        or 0x2200 <= code <= 0x22FF
        or 0x27C0 <= code <= 0x27EF
        or 0x2980 <= code <= 0x29FF
    )


def _pdfs(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    return sorted(target.glob("*.pdf"))


def scan(target: Path) -> list[dict]:
    stats: dict[str, dict] = {}
    for pdf in _pdfs(target):
        doc = pymupdf.open(pdf)
        try:
            for page in doc:
                for block in page.get_text("dict")["blocks"]:
                    for line in block.get("lines", []):
                        for span in line.get("spans", []):
                            font = span.get("font") or ""
                            text = span.get("text") or ""
                            if not font or not text:
                                continue
                            item = stats.setdefault(
                                font,
                                {
                                    "font": font,
                                    "face": resolve_font(font).to_dict(),
                                    "chars": 0,
                                    "math_chars": 0,
                                    "papers": set(),
                                },
                            )
                            item["chars"] += len(text)
                            item["papers"].add(pdf.name)
                            if any(_is_math_codepoint(ch) for ch in text):
                                item["math_chars"] += len(text)
        finally:
            doc.close()

    rows = sorted(stats.values(), key=lambda row: row["chars"], reverse=True)
    for row in rows:
        row["papers"] = sorted(row["papers"])
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path, help="PDF 文件或包含 PDF 的目录")
    parser.add_argument("--json", type=Path, help="把完整结果写入 JSON")
    parser.add_argument(
        "--unregistered-only", action="store_true",
        help="只列 UNKNOWN 角色（未注册字体）",
    )
    args = parser.parse_args()

    rows = scan(args.target)
    if args.unregistered_only:
        rows = [row for row in rows if row["face"]["role"] == FontRole.UNKNOWN.value]

    print(f"{'role':8} {'chars':>8} {'math':>8} {'papers':>6}  font")
    for row in rows:
        print(
            f"{row['face']['role']:8} {row['chars']:8d} {row['math_chars']:8d}"
            f" {len(row['papers']):6d}  {row['font']}"
        )
    if args.json:
        args.json.write_text(
            json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"\nWROTE {args.json}")

    unknown = [
        row for row in rows
        if row["face"]["role"] == FontRole.UNKNOWN.value
    ]
    if unknown:
        chars = sum(row["chars"] for row in unknown)
        print(f"\n未注册字体 {len(unknown)} 个，共 {chars} 字符；可据此补注册表。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())