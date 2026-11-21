"""P1 验证：逐页回填 + 硬性指标验收 + 第 6 页观感出图。"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pymupdf

from paperead.pdf_export import load_blocks, pick_font, render_page

sys.stdout.reconfigure(encoding="utf-8")

PID = "p_efcf4e4aacb6"
PDF = Path(f"data/papers/{PID}.pdf")
OUT = Path(".tmp/p1_out")
PAGES_DIR = OUT / "pages"
PAGES_DIR.mkdir(parents=True, exist_ok=True)
blocks = load_blocks(f"data/papers/{PID}.blocks.json")
font = pick_font()

src = pymupdf.open(PDF)
page_count = src.page_count
orig_sizes = {i: (round(src[i].rect.width, 1), round(src[i].rect.height, 1)) for i in range(page_count)}
src.close()

results = []
skipped: Counter[str] = Counter()
t0 = time.time()
for i in range(page_count):
    out = PAGES_DIR / f"p{i + 1}.pdf"
    res = render_page(PDF, blocks, i, out, font_path=font)
    results.append(res)
    for item in res.skipped:
        skipped[item["reason"]] += 1
print(f"RENDERED PAGES {page_count} in {round(time.time() - t0, 1)}s")

all_layouts = [x for r in results for x in r.layouts]
print(f"BLOCKS rendered={len(all_layouts)} skipped={dict(skipped)}")

if all_layouts:
    sizes = sorted(x.font_size for x in all_layouts)
    over = [x for x in all_layouts if x.overflow]
    min_size = [x for x in all_layouts if x.font_size <= 8.0]
    print(f"FONT SIZE min={sizes[0]} p50={sizes[len(sizes)//2]} max={sizes[-1]}")
    print(f"OVERFLOW={len(over)} AT_MIN_SIZE={len(min_size)}")
    for x in sorted(over, key=lambda v: -v.needed / max(v.available, 1))[:5]:
        print(f"  OVER {x.block_id} {x.kind} need={x.needed} avail={x.available} size={x.font_size}")

residue = [(r.page + 1, b) for r in results for b in r.residue_blocks]
print(f"RESIDUE BLOCKS {len(residue)} {residue[:10]}")

bad_size = [r.page + 1 for r in results if (round(r.page_size[0], 1), round(r.page_size[1], 1)) != orig_sizes[r.page]]
bad_img = [(r.page + 1, r.images_before, r.images_after) for r in results if r.images_before != r.images_after]
bad_draw = [(r.page + 1, r.drawings_before, r.drawings_after) for r in results if r.drawings_before != r.drawings_after]
no_font = [r.page + 1 for r in results if r.layouts and not any("SimSun" in f[3] for f in r.fonts)]
print("PAGE SIZE MISMATCH", bad_size)
print("IMAGES CHANGED", bad_img)
print("DRAWINGS CHANGED", bad_draw)
print("FONT MISSING", no_font)

cjk = {}
for i in range(page_count):
    d = pymupdf.open(PAGES_DIR / f"p{i + 1}.pdf")
    t = d[0].get_text()
    cjk[i + 1] = sum(1 for ch in t if "\u4e00" <= ch <= "\u9fff")
    d.close()
print("CJK PER PAGE", cjk, "TOTAL", sum(cjk.values()))

# 第 6 页出图（推荐策略 + 全回填对照）
for tag, skip_nested in (("recommended", True), ("full", False)):
    out = OUT / f"page6_{tag}.pdf"
    res = render_page(PDF, blocks, 5, out, font_path=font, skip_nested_math=skip_nested)
    d = pymupdf.open(out)
    p = d[0]
    head = " ".join(p.get_text().split())[:140]
    print(f"[{tag}] rendered={len(res.layouts)} skipped={[x['reason'] for x in res.skipped]} "
          f"restored={res.restored_keep_blocks} residue={res.residue_blocks} head={head}")
    p.get_pixmap(matrix=pymupdf.Matrix(2, 2)).save(OUT / f"page6_{tag}.png")
    d.close()

report = {
    "paper": PID,
    "pages": page_count,
    "rendered_blocks": len(all_layouts),
    "skipped": dict(skipped),
    "overflow": len([x for x in all_layouts if x.overflow]),
    "at_min_size": len([x for x in all_layouts if x.font_size <= 8.0]),
    "residue_blocks": residue,
    "page_size_mismatch": bad_size,
    "images_changed": bad_img,
    "drawings_changed": bad_draw,
    "font_missing": no_font,
    "cjk_per_page": cjk,
    "pages_detail": [r.to_dict() for r in results],
}
(OUT / "p1_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
print("REPORT", (OUT / "p1_report.json").resolve())
print("PNG", (OUT / "page6_recommended.png").resolve())