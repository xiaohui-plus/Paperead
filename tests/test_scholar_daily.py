"""每日推荐算法测试。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from paperead.scholar import daily
from paperead.scholar.daily import (
    build_category_or_query,
    build_openalex_subfield_filter,
    daily_batch,
    heat_score,
    mulberry32,
    rank_daily,
)
from paperead.scholar.models import Paper

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)


def make_paper(pid: str, *, hours_ago: float, citations: int | None = None, cat: str = "cs.LG") -> Paper:
    published = (NOW - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z")
    return Paper(
        id=pid,
        source="arxiv",
        title=f"Paper {pid}",
        category=cat,
        published_at=published,
        citations=citations,
    )


def test_heat_score_decays_with_age_and_grows_with_citations():
    new = make_paper("a", hours_ago=1)
    old = make_paper("b", hours_ago=24 * 30)
    assert heat_score(new.published_at, NOW) > heat_score(old.published_at, NOW)

    cited = make_paper("c", hours_ago=10, citations=1000)
    plain = make_paper("d", hours_ago=10, citations=0)
    assert heat_score(cited.published_at, NOW, cited.citations) > heat_score(
        plain.published_at, NOW, plain.citations
    )


def test_heat_score_missing_date_is_zero():
    assert heat_score(None, NOW) == 0.0
    assert heat_score("not-a-date", NOW) == 0.0


def test_rank_daily_is_stable_for_same_seed():
    papers = [make_paper(f"p{i}", hours_ago=i * 3, cat="cs.AI" if i % 2 else "cs.CL") for i in range(10)]
    first = [p.id for p in rank_daily(papers, NOW, seed=42, limit=10)]
    second = [p.id for p in rank_daily(list(reversed(papers)), NOW, seed=42, limit=10)]
    assert first == second


def test_rank_daily_rotates_categories():
    papers = [make_paper(f"a{i}", hours_ago=i + 1, cat="A") for i in range(3)]
    papers += [make_paper(f"b{i}", hours_ago=i + 1, cat="B") for i in range(3)]
    ids = [p.category for p in rank_daily(papers, NOW, seed=7, limit=6)]
    # 每个分类轮流取一条：两个分类时严格交替，不会出现同类连排
    assert ids.count("A") == 3 and ids.count("B") == 3
    assert all(a != b for a, b in zip(ids, ids[1:]))
    assert all(not (ids[i] == ids[i + 1] == ids[i + 2]) for i in range(len(ids) - 2))


def test_daily_batch_windows_do_not_overlap():
    queue = [make_paper(f"p{i}", hours_ago=i) for i in range(30)]
    first = [p.id for p in daily_batch(queue, 0, limit=12)]
    second = [p.id for p in daily_batch(queue, 1, limit=12)]
    assert len(first) == 12
    assert not set(first) & set(second)


def test_daily_batch_wraps_around():
    queue = [make_paper(f"p{i}", hours_ago=i) for i in range(12)]
    assert [p.id for p in daily_batch(queue, 0, 12)] == [p.id for p in daily_batch(queue, 3, 12)]


def test_daily_batch_handles_empty_queue():
    assert daily_batch([], 0) == []


def test_category_query_builders():
    assert build_category_or_query(["cs.AI", "cs.LG"]) == "%28cat:cs.AI+OR+cat:cs.LG%29"
    assert build_openalex_subfield_filter([1702, 1707]) == "primary_topic.subfield.id:1702|1707"


def test_mulberry32_is_deterministic_and_in_range():
    a = [mulberry32(123)() for _ in range(3)]
    b = [mulberry32(123)() for _ in range(3)]
    assert a == b
    assert all(0 <= x < 1 for x in a)


def test_get_daily_drops_future_dated_entries(monkeypatch):
    """OpenAlex 会把在线优先的出版日期写到未来，不能霸占首屏。"""
    future = make_paper("future", hours_ago=-24 * 400)
    past = make_paper("past", hours_ago=5)

    async def fake_pool(_date_key):
        return [future, past]

    monkeypatch.setattr(daily, "_pool", fake_pool)
    result = asyncio.run(daily.get_daily(0))
    assert [p["id"] for p in result["papers"]] == ["past"]
    assert result["pool_size"] == 1


def test_is_future_tolerance():
    assert daily._is_future((NOW + timedelta(days=30)).isoformat(), NOW)
    assert not daily._is_future((NOW + timedelta(days=1)).isoformat(), NOW)
    assert not daily._is_future(None, NOW)
    assert not daily._is_future("garbage", NOW)
