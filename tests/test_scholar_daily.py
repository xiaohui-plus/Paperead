"""每日推荐算法测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

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

@pytest.fixture()
def daily_cache(tmp_path):
    folder = tmp_path / "scholar_daily"
    daily.configure_cache_dir(folder)
    daily.reset_cache()
    yield folder
    daily.configure_cache_dir(None)
    daily.reset_cache()


def make_pool(count: int) -> list[Paper]:
    return [make_paper(f"p{i}", hours_ago=i + 1) for i in range(count)]


def test_prefetch_writes_timestamped_cache(daily_cache, monkeypatch):
    calls = {"count": 0}

    async def fake_fetch():
        calls["count"] += 1
        return make_pool(30)

    monkeypatch.setattr(daily, "_fetch_pool", fake_fetch)
    info = asyncio.run(daily.prefetch_daily())

    assert info["cached"] is False
    assert info["pool_size"] == 30
    assert calls["count"] == 1

    files = sorted(daily_cache.glob("*.json"))
    assert len(files) == 1
    assert files[0].name.startswith(f"{info['date']}_")
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["date"] == info["date"]
    assert payload["fetched_at"]
    assert payload["saved_at"]
    assert len(payload["papers"]) == 30


def test_same_day_reuses_cache_without_refetch(daily_cache, monkeypatch):
    calls = {"count": 0}

    async def fake_fetch():
        calls["count"] += 1
        return make_pool(30)

    monkeypatch.setattr(daily, "_fetch_pool", fake_fetch)
    first = asyncio.run(daily.prefetch_daily())
    daily.reset_cache()
    second = asyncio.run(daily.prefetch_daily())

    assert first["cached"] is False
    assert second["cached"] is True
    assert second["fetched_at"] == first["fetched_at"]
    assert calls["count"] == 1
    assert len(list(daily_cache.glob("*.json"))) == 1


def test_new_day_fetches_again(daily_cache, monkeypatch):
    keys = iter(["2026-11-14", "2026-11-15"])
    monkeypatch.setattr(daily, "_today_key", lambda: next(keys))
    calls = {"count": 0}

    async def fake_fetch():
        calls["count"] += 1
        return make_pool(20)

    monkeypatch.setattr(daily, "_fetch_pool", fake_fetch)
    first = asyncio.run(daily.prefetch_daily())
    daily.reset_cache()
    second = asyncio.run(daily.prefetch_daily())

    assert first["date"] == "2026-11-14"
    assert second["date"] == "2026-11-15"
    assert first["cached"] is False and second["cached"] is False
    assert calls["count"] == 2
    assert len(list(daily_cache.glob("*.json"))) == 2


def test_corrupt_cache_is_ignored(daily_cache, monkeypatch):
    daily.reset_cache()
    daily_cache.mkdir(parents=True, exist_ok=True)
    date_key = daily._today_key()
    (daily_cache / f"{date_key}_000000.json").write_text("not json", encoding="utf-8")
    calls = {"count": 0}

    async def fake_fetch():
        calls["count"] += 1
        return make_pool(16)

    monkeypatch.setattr(daily, "_fetch_pool", fake_fetch)
    info = asyncio.run(daily.prefetch_daily())

    assert info["cached"] is False
    assert calls["count"] == 1
    files = list(daily_cache.glob("*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text(encoding="utf-8"))["pool_size"] == 16


def test_daily_rounds_rotate_within_cached_pool(monkeypatch):
    daily.reset_cache()

    async def fake_pool(_date_key):
        return make_pool(120)

    monkeypatch.setattr(daily, "_pool", fake_pool)
    seen: list[str] = []
    for round_index in range(10):
        result = asyncio.run(daily.get_daily(round_index))
        ids = [p["id"] for p in result["papers"]]
        assert len(ids) == 12
        assert result["pool_size"] == 120
        seen.extend(ids)

    assert len(seen) == len(set(seen)) == 120
