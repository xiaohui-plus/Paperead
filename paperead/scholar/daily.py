"""每日推荐：没有关键词时的默认内容。

排序是「热度 × 时间衰减 + 分类均衡轮转」：
    score = (1 + ln(1 + citations)) / (ageHours + 2) ** 1.5
被引量做对数压缩，避免高被引老论文碾压新论文；时间衰减保证「今日」新鲜。
同一天用日期做种子，结果稳定；「换一批」只是在这条全天稳定的队列上向后切窗口，
所以相邻两批内容不重叠。

算法与参考项目（TypeScript 版）逐行对应，便于两个实现互相对照。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from datetime import datetime, timezone

from .http import CONTACT_EMAIL, FriendlyError, arxiv_fetch, fetch_text
from .models import Paper
from .sources import (
    ARXIV_API,
    OPENALEX_API,
    OPENALEX_SELECT,
    parse_arxiv_entries,
    parse_openalex_works,
)

log = logging.getLogger(__name__)

# 两个学科方向，各自对应的 arXiv 分类与 OpenAlex 子学科 id。
DAILY_BUCKETS: list[dict] = [
    {
        "k": "ai",
        "l": "人工智能与机器学习",
        "cats": ["cs.AI", "cs.LG", "cs.CL", "cs.CV", "cs.RO"],
        # 1702 人工智能 / 1707 计算机视觉 / 1703 计算理论 / 1706 计算机应用 / 1712 软件
        "subfields": [1702, 1707, 1703, 1706, 1712],
    },
    {
        "k": "bio",
        "l": "生物医学与交叉",
        "cats": ["q-bio.NC", "q-bio.QM", "q-bio.BM", "q-bio.PE", "eess.IV", "eess.SP", "q-fin.ST"],
        # 1312 分子生物学 / 1311 遗传学 / 1306 癌症研究 / 1313 分子医学 / 1305 生物技术 / 1307 细胞生物学
        "subfields": [1312, 1311, 1306, 1313, 1305, 1307],
    },
]

DAILY_LIMIT = 12
# 只取近 45 天的 OpenAlex 论文，保证推荐流仍「新鲜」，同时带上被引量。
OPENALEX_POOL_DAYS = 45
HEAT_GRAVITY = 1.5


# ── 纯函数 ──────────────────────────────────────────────────


def build_category_or_query(cats: list[str]) -> str:
    """arXiv 的 OR 分组查询，例如 (cat:cs.AI+OR+cat:cs.LG)。"""
    body = "+OR+".join(f"cat:{c}" for c in cats)
    return f"%28{body}%29"


def build_openalex_subfield_filter(subfields: list[int]) -> str:
    return "primary_topic.subfield.id:" + "|".join(str(s) for s in subfields)


def to_date_key(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d")


def hash_seed(text: str) -> int:
    """字符串 → 32 位无符号种子（FNV-1a 变体）。"""
    h = 2166136261
    for ch in text:
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    return h


def mulberry32(seed: int):
    """小而稳定的 32 位 PRNG，返回 [0,1) 的生成器。"""
    state = seed & 0xFFFFFFFF

    def rnd() -> float:
        nonlocal state
        state = (state + 0x6D2B79F5) & 0xFFFFFFFF
        t = state
        t = ((t ^ (t >> 15)) * (t | 1)) & 0xFFFFFFFF
        t ^= (t + (((t ^ (t >> 7)) * (t | 61)) & 0xFFFFFFFF)) & 0xFFFFFFFF
        t &= 0xFFFFFFFF
        return ((t ^ (t >> 14)) & 0xFFFFFFFF) / 4294967296

    return rnd


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    if len(text) == 10:  # 只有日期
        text += "T00:00:00+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def heat_score(
    published_iso: str | None, now: datetime, citations: int | None = None
) -> float:
    published = _parse_iso(published_iso)
    if published is None:
        return 0.0
    age_hours = max(0.0, (now - published).total_seconds() / 3600)
    impact = 1.0 + math.log(1.0 + max(0, citations or 0))
    return impact / ((age_hours + 2.0) ** HEAT_GRAVITY)


def rank_daily(
    papers: list[Paper], now: datetime, seed: int, limit: int | None = None
) -> list[Paper]:
    """按热度打分，再按分类轮转取用，避免同类扎堆。"""
    limit = DAILY_LIMIT if limit is None else limit
    rnd = mulberry32(seed)
    jitter = {p.id: rnd() for p in papers}

    scored = sorted(
        (
            (
                p,
                p.category or "arXiv",
                heat_score(p.published_at, now, p.citations) * (1 + 0.15 * jitter[p.id]),
            )
            for p in papers
        ),
        # 稳定性：热度相近时用 id 兜底比较，保证同种子输出完全可复现
        key=lambda row: (-row[2], row[0].id),
    )

    by_cat: dict[str, list[tuple[Paper, str, float]]] = {}
    for row in scored:
        by_cat.setdefault(row[1], []).append(row)
    order = sorted(
        by_cat,
        key=lambda cat: (-by_cat[cat][0][2], cat),
    )

    out: list[Paper] = []
    seen: set[str] = set()
    while len(out) < limit:
        progressed = False
        for cat in order:
            bucket = by_cat[cat]
            while bucket:
                paper = bucket.pop(0)[0]
                progressed = True
                if paper.id in seen:
                    continue
                seen.add(paper.id)
                out.append(paper)
                break
            if len(out) >= limit:
                break
        if not progressed:
            break
    return out


def daily_batch(queue: list[Paper], round_index: int, limit: int = DAILY_LIMIT) -> list[Paper]:
    """「换一批」：把全天稳定的队列按轮次切成窗口，相邻两批不重叠。"""
    size = min(limit, len(queue))
    if size <= 0:
        return []
    cycles = max(1, math.ceil(len(queue) / size))
    start = (((round_index % cycles) + cycles) % cycles) * size % len(queue)
    return [queue[(start + i) % len(queue)] for i in range(size)]

# ── 取候选池 ────────────────────────────────────────────────


async def _arxiv_pool(cats: list[str]) -> list[Paper]:
    url = (
        f"{ARXIV_API}?search_query={build_category_or_query(cats)}"
        "&sortBy=submittedDate&sortOrder=descending&start=0&max_results=60"
    )
    # 分类 OR 查询偶发较慢，且候选池每天只拉一次，放宽超时换更稳的首屏。
    xml = await arxiv_fetch(url, timeout=20, retries=1)
    return parse_arxiv_entries(xml)


async def _openalex_pool(subfields: list[int]) -> list[Paper]:
    since = datetime.now(timezone.utc).timestamp() - OPENALEX_POOL_DAYS * 86400
    from_date = datetime.fromtimestamp(since, tz=timezone.utc).strftime("%Y-%m-%d")
    params = (
        f"filter={build_openalex_subfield_filter(subfields)},from_publication_date:{from_date}"
        f"&sort=publication_date:desc&per-page=50&select={OPENALEX_SELECT}"
        f"&mailto={CONTACT_EMAIL}"
    )
    payload = json.loads(await fetch_text(f"{OPENALEX_API}?{params}", timeout=20, retries=1))
    return parse_openalex_works(payload)


# 按天缓存：同一实例一天内最多向外拉取一次候选池。
_day_date = ""
_day_papers: list[Paper] = []


async def _pool(date_key: str) -> list[Paper]:
    global _day_date, _day_papers
    if _day_date == date_key and _day_papers:
        return _day_papers

    tasks = [_arxiv_pool(b["cats"]) for b in DAILY_BUCKETS]
    tasks += [_openalex_pool(b["subfields"]) for b in DAILY_BUCKETS]
    settled = await asyncio.gather(*tasks, return_exceptions=True)

    merged: list[Paper] = []
    seen: set[str] = set()
    for item in settled:
        if isinstance(item, BaseException):
            log.warning("每日推荐候选池失败：%s", item)
            continue
        for paper in item:
            if paper.id in seen:
                continue
            seen.add(paper.id)
            merged.append(paper)
    if not merged:
        raise FriendlyError("推荐内容加载失败，请稍后重试。", True)

    _day_date = date_key
    _day_papers = merged
    return merged


async def get_daily(round_index: int = 0) -> dict:
    now = datetime.now(timezone.utc)
    date_key = to_date_key(now)
    pool = await _pool(date_key)
    # OpenAlex 偶尔把「在线优先」的出版日期写到未来一两年，这类条目会霸占首屏，
    # 推荐流只保留今天为止的内容（留 2 天容差）。
    fresh = [p for p in pool if not _is_future(p.published_at, now)]
    queue = fresh or pool
    ranked = rank_daily(queue, now, hash_seed(date_key), limit=len(queue))
    return {
        "papers": [p.to_dict() for p in daily_batch(ranked, round_index, DAILY_LIMIT)],
        "date": date_key,
        "pool_size": len(queue),
        "error": None,
    }


def _is_future(published_iso: str | None, now: datetime, tolerance_days: int = 2) -> bool:
    published = _parse_iso(published_iso)
    if published is None:
        return False
    return (published - now).days > tolerance_days
