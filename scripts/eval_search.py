"""本地检索回归评测。

用法：
  .venv-dev\\Scripts\\python.exe scripts\\eval_search.py
  .venv-dev\\Scripts\\python.exe scripts\\eval_search.py --labels scripts\\eval_labels.json

`--labels` 是 {query: [paperId, ...]} 的人工标注文件；没有标注时只统计
覆盖度、尾部噪声与最佳页码命中，不计算 Recall/MRR。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.research.search import LibrarySearch, _concepts, _matched  # noqa: E402
from server.store import LibraryStore  # noqa: E402

QUERIES = [
    "LNP extrahepatic delivery",
    "CRISPR lipid nanoparticle",
    "immunotherapy resistance hepatocellular",
    "mRNA vaccine delivery",
    "dual-target CAR-T",
    "blood-brain barrier nanoparticle",
    "gene editing off-target",
    "protein degradation PROTAC",
    "single-cell transcriptomics tumor",
    "AAV capsid engineering",
]


def page_hit(library: LibraryStore, entry: dict, concepts: list[str]) -> bool:
    page = entry.get("page")
    if not page:
        return False
    blocks = library.read_blocks(entry["paperId"]) or []
    text = " ".join(str(block.get("text") or "") for block in blocks if block.get("page") == page)
    return len(_matched(concepts, text)) >= max(1, len(concepts) // 2)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--labels", help="JSON: {query: [paperId, ...]}")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--queries", help="JSON list of queries, overrides the built-in list")
    args = parser.parse_args()

    data = ROOT / "data"
    library = LibraryStore(data)
    search = LibrarySearch(library, data / "research" / "search.db")

    queries = QUERIES
    if args.queries:
        queries = json.loads(Path(args.queries).read_text(encoding="utf-8"))
    labels = {}
    if args.labels:
        labels = json.loads(Path(args.labels).read_text(encoding="utf-8"))

    started = time.monotonic()
    search.refresh()
    build_ms = int((time.monotonic() - started) * 1000)

    rows = []
    latencies: list[int] = []
    recalls: list[float] = []
    reciprocal: list[float] = []
    page_hits = 0
    noise = 0
    total = 0

    for query in queries:
        began = time.monotonic()
        payload = search.search_page(query, limit=args.limit)
        latencies.append(int((time.monotonic() - began) * 1000))
        results = payload["results"]
        concepts = _concepts(query)
        total += len(results)
        noise += sum(1 for entry in results if entry["coverage"] < 0.6)
        page_hits += sum(1 for entry in results[:3] if page_hit(library, entry, concepts))

        if query in labels:
            relevant = set(labels[query])
            found = [entry["paperId"] for entry in results]
            recalls.append(len(relevant & set(found)) / len(relevant) if relevant else 0.0)
            rank = next((index + 1 for index, pid in enumerate(found) if pid in relevant), 0)
            reciprocal.append(1.0 / rank if rank else 0.0)

        top = results[0] if results else {}
        rows.append(
            {
                "query": query,
                "kept": len(results),
                "noise": sum(1 for entry in results if entry["coverage"] < 0.6),
                "medianCoverage": payload["stats"]["medianCoverage"],
                "top": (top.get("title") or "")[:44],
                "page": top.get("page"),
            }
        )

    print(f"index_build_ms={build_ms}")
    print(f"{'query':38} {'kept':>4} {'noise':>5} {'medCov':>7} {'page':>4}  top")
    for row in rows:
        print(
            f"{row['query'][:38]:38} {row['kept']:>4} {row['noise']:>5} "
            f"{row['medianCoverage']:>7} {str(row['page']):>4}  {row['top']}"
        )

    print()
    print(f"noise_rate={noise / total:.3f}" if total else "noise_rate=0")
    print(f"page_hit_at_3={page_hits / max(1, len(queries) * 3):.3f}")
    if latencies:
        ordered = sorted(latencies)
        p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
        print(f"latency_ms p50={statistics.median(latencies):.0f} p95={p95} max={ordered[-1]}")
    if recalls:
        print(f"recall@10={statistics.mean(recalls):.3f} mrr@10={statistics.mean(reciprocal):.3f}")


if __name__ == "__main__":
    main()
