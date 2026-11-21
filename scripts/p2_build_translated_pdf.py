"""P2：页级译文 PDF 生成 + 锚点缓存 + 整篇合并。

产物布局（与既有 ``{id}_assets`` 并列，避免和 ``{id}.pdf`` 冲突）::

    data/papers/{id}_translated/{lang}/
        p1.pdf ... pN.pdf       # 单页译文，页尺寸与原页一致
        p1.anchors.json ...     # 双栏同步用的块锚点
        meta.json               # parse_hash / algo_ver / 语言 / 页数
        translated.pdf          # 按页序合并的整篇
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pymupdf

from paperead.pdf_export import _rect, load_blocks, pick_body_font, render_page

sys.stdout.reconfigure(encoding="utf-8")

ALGO_VER = "p2-band-v1"
PID = "p_efcf4e4aacb6"
LANG = "zh-Hans"
BASE = Path("data/papers")
OUT = BASE / f"{PID}_translated" / LANG
PREVIEW = Path(".tmp/p2_out")


def main() -> int:
    pdf_path = BASE / f"{PID}.pdf"
    blocks_path = BASE / f"{PID}.blocks.json"
    OUT.mkdir(parents=True, exist_ok=True)
    PREVIEW.mkdir(parents=True, exist_ok=True)

    blocks = load_blocks(blocks_path)
    parse_hash = hashlib.sha256(blocks_path.read_bytes()).hexdigest()[:12]
    font = pick_body_font()

    src = pymupdf.open(pdf_path)
    page_count = src.page_count
    sizes = [(round(src[i].rect.width, 1), round(src[i].rect.height, 1)) for i in range(page_count)]
    src.close()

    meta_path = OUT / "meta.json"
    force = "--force" in sys.argv
    if not force and meta_path.exists():
        try:
            old = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            old = {}
        cached = (
            old.get("parse_hash") == parse_hash
            and old.get("algo_ver") == ALGO_VER
            and old.get("lang") == LANG
            and old.get("pages") == page_count
            and (OUT / "translated.pdf").exists()
            and all((OUT / f"p{i + 1}.pdf").exists() for i in range(page_count))
        )
        if cached:
            print(f"CACHE HIT {OUT / 'translated.pdf'} parse_hash={parse_hash} algo={ALGO_VER}")
            return 0

    t0 = time.time()
    totals = {"blocks": 0, "overflow": 0, "residue": 0, "cjk": 0}
    page_reports: list[dict] = []

    for index in range(page_count):
        page_pdf = OUT / f"p{index + 1}.pdf"
        res = render_page(pdf_path, blocks, index, page_pdf, font_path=font)
        anchors = []
        for layout in res.layouts:
            block = next((b for b in blocks if str(b.get("id")) == layout.block_id), None)
            rect = _rect(block, index) if block else None
            if rect is None:
                continue
            anchors.append(
                {
                    "block_id": layout.block_id,
                    "kind": layout.kind,
                    "page": index,
                    "rect": [round(v, 1) for v in rect],
                    "font_size": layout.font_size,
                    "lines": layout.lines,
                }
            )
        (OUT / f"p{index + 1}.anchors.json").write_text(
            json.dumps({"page": index, "blocks": anchors}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )

        doc = pymupdf.open(page_pdf)
        text = doc[0].get_text()
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        doc.close()

        totals["blocks"] += len(res.layouts)
        totals["overflow"] += sum(1 for x in res.layouts if x.overflow)
        totals["residue"] += len(res.residue_blocks)
        totals["cjk"] += cjk
        page_reports.append(
            {
                "page": index + 1,
                "blocks": len(res.layouts),
                "overflow": [x.block_id for x in res.layouts if x.overflow],
                "min_size": min((x.font_size for x in res.layouts), default=0),
                "residue": res.residue_blocks,
                "cjk": cjk,
                "drawings": [res.drawings_before, res.drawings_after],
                "images": [res.images_before, res.images_after],
            }
        )
        print(
            f"PAGE {index + 1}/{page_count} blocks={len(res.layouts)} "
            f"overflow={len([x for x in res.layouts if x.overflow])} cjk={cjk}"
        )

    merged = pymupdf.open()
    for index in range(page_count):
        doc = pymupdf.open(OUT / f"p{index + 1}.pdf")
        merged.insert_pdf(doc)
        doc.close()
    try:
        merged.subset_fonts()
    except Exception:
        pass
    merged.save(OUT / "translated.pdf", garbage=4, deflate=True, clean=True)
    merged.close()

    # 校验：页数 / 页尺寸
    check = pymupdf.open(OUT / "translated.pdf")
    merged_sizes = [(round(check[i].rect.width, 1), round(check[i].rect.height, 1)) for i in range(check.page_count)]
    check.close()
    size_ok = merged_sizes == sizes

    meta = {
        "paper_id": PID,
        "lang": LANG,
        "algo_ver": ALGO_VER,
        "parse_hash": parse_hash,
        "pages": page_count,
        "generated_at": int(time.time()),
        "page_size_ok": size_ok,
        "totals": totals,
    }
    (OUT / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "report.json").write_text(
        json.dumps({"meta": meta, "pages": page_reports}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )

    print(f"MERGED {OUT / 'translated.pdf'}")
    print(f"PAGES {page_count} size_ok={size_ok} blocks={totals['blocks']} "
          f"overflow={totals['overflow']} residue={totals['residue']} cjk={totals['cjk']} "
          f"elapsed={round(time.time() - t0, 1)}s")

    # 预览图：第 2/6/7 页（正文 / 公式 / 表格）
    for page_no in (2, 6, 7):
        doc = pymupdf.open(OUT / f"p{page_no}.pdf")
        doc[0].get_pixmap(matrix=pymupdf.Matrix(2, 2)).save(PREVIEW / f"page{page_no}.png")
        doc.close()
    src = pymupdf.open(pdf_path)
    for page_no in (2, 6, 7):
        src[page_no - 1].get_pixmap(matrix=pymupdf.Matrix(2, 2)).save(PREVIEW / f"page{page_no}_orig.png")
    src.close()
    print("PREVIEW", PREVIEW.resolve())
    return 0 if size_ok and totals["residue"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())