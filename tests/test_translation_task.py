"""Phase 1/3：翻译任务模型、自适应并发与长块切分。"""

from __future__ import annotations

import asyncio

from paperead.translation_task import (
    AdaptiveLimiter,
    new_job,
    public_job,
    retry_delay,
    split_text,
)


def test_split_text_keeps_content_and_limits_chunks() -> None:
    text = "\n\n".join(f"Paragraph {i}. " + "word " * 200 for i in range(4))
    chunks = split_text(text, max_chars=500)
    assert len(chunks) > 1
    assert all(len(chunk) <= 500 for chunk in chunks)
    assert "Paragraph 0" in chunks[0]
    assert "word" in chunks[-1]


def test_public_job_uses_published_cursor() -> None:
    job = new_job("p_x", "zh-Hans", total=5)
    job["translated_ids"] = ["a", "b", "c"]
    job["published_ids"] = ["a", "b"]
    job["done"] = 3
    view = public_job(job)
    assert view["job_id"] == job["job_id"]
    assert view["total"] == 5
    assert view["done"] == 3
    assert view["pending"] == 2
    assert view["next_cursor"] == 2


def test_adaptive_limiter_respects_limit_and_recovers() -> None:
    async def run() -> tuple[int, int]:
        limiter = AdaptiveLimiter(2, min_limit=1, max_limit=4)
        active = 0
        peak = 0

        async def work() -> None:
            nonlocal active, peak
            async with limiter:
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.005)
                active -= 1

        await asyncio.gather(*(work() for _ in range(8)))
        return peak, limiter.limit

    peak, limit = asyncio.run(run())
    assert peak <= 2
    assert limit == 2

    limiter = AdaptiveLimiter(8, min_limit=2, max_limit=12)
    limiter.on_error("rate_limit")
    assert limiter.limit == 4
    limiter.on_error("rate_limit")
    assert limiter.limit == 2
    limiter.on_error("timeout")
    assert limiter.limit == 2
    for _ in range(8):
        limiter.on_success()
    assert limiter.limit == 3


def test_retry_delay_rate_limit_floor() -> None:
    assert retry_delay(0, "network") >= 1.2
    assert retry_delay(0, "rate_limit") >= 3.0
    assert retry_delay(2, "unknown") <= 15.0