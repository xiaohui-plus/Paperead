"""网络层测试：缓存、并发合并、重试、arXiv 节流。"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from paperead.scholar import http


def run(coro):
    return asyncio.run(coro)


def test_cache_merges_concurrent_requests(monkeypatch):
    cache = http.RequestCache()
    calls = {"n": 0}

    async def run_task():
        calls["n"] += 1
        await asyncio.sleep(0.02)
        return "payload"

    async def main():
        return await asyncio.gather(*(cache.get("k", 60, run_task) for _ in range(5)))

    assert run(main()) == ["payload"] * 5
    assert calls["n"] == 1
    assert len(cache) == 1


def test_cache_expires_after_ttl():
    cache = http.RequestCache()
    calls = {"n": 0}

    async def run_task():
        calls["n"] += 1
        return "payload"

    async def main():
        await cache.get("k", 0.01, run_task)
        await asyncio.sleep(0.03)
        await cache.get("k", 0.01, run_task)

    run(main())
    assert calls["n"] == 2


def test_cache_does_not_store_failures():
    cache = http.RequestCache()

    async def boom():
        raise http.FriendlyError("炸了", True)

    with pytest.raises(http.FriendlyError):
        run(cache.get("k", 60, boom))
    assert len(cache) == 0


def test_cache_reuses_inflight_error_for_waiters():
    cache = http.RequestCache()

    async def boom():
        await asyncio.sleep(0.01)
        raise http.FriendlyError("炸了", True)

    async def main():
        return await asyncio.gather(
            cache.get("k", 60, boom), cache.get("k", 60, boom), return_exceptions=True
        )

    results = run(main())
    assert all(isinstance(r, http.FriendlyError) for r in results)
    assert len(cache) == 0


def test_fetch_raw_retries_retryable_status(monkeypatch):
    monkeypatch.setattr(http, "_backoff", lambda attempt: 0)
    responses = [httpx.Response(503), httpx.Response(429), httpx.Response(200, text="ok")]
    calls = {"n": 0}

    async def fake_once(url, timeout=http.TIMEOUT_S, headers=None):
        calls["n"] += 1
        return responses.pop(0)

    monkeypatch.setattr(http, "fetch_once", fake_once)
    assert run(http.fetch_raw("https://example.org/x", 1, 2)) == "ok"
    assert calls["n"] == 3


def test_fetch_raw_gives_up_after_retries(monkeypatch):
    monkeypatch.setattr(http, "_backoff", lambda attempt: 0)
    calls = {"n": 0}

    async def fake_once(url, timeout=http.TIMEOUT_S, headers=None):
        calls["n"] += 1
        return httpx.Response(503)

    monkeypatch.setattr(http, "fetch_once", fake_once)
    with pytest.raises(http.FriendlyError) as excinfo:
        run(http.fetch_raw("https://example.org/x", 1, 2))
    assert calls["n"] == 3
    assert "暂时不可用" in excinfo.value.message


def test_fetch_raw_does_not_retry_client_errors(monkeypatch):
    calls = {"n": 0}

    async def fake_once(url, timeout=http.TIMEOUT_S, headers=None):
        calls["n"] += 1
        return httpx.Response(400)

    monkeypatch.setattr(http, "fetch_once", fake_once)
    with pytest.raises(http.FriendlyError):
        run(http.fetch_raw("https://example.org/x", 1, 2))
    assert calls["n"] == 1


def test_fetch_raw_auth_error_is_not_retried(monkeypatch):
    calls = {"n": 0}

    async def fake_once(url, timeout=http.TIMEOUT_S, headers=None):
        calls["n"] += 1
        return httpx.Response(401)

    monkeypatch.setattr(http, "fetch_once", fake_once)
    with pytest.raises(http.FriendlyError) as excinfo:
        run(http.fetch_raw("https://example.org/x", 1, 2))
    assert calls["n"] == 1
    assert "API Key" in excinfo.value.message


def test_fetch_raw_timeout_is_not_retried(monkeypatch):
    calls = {"n": 0}

    async def fake_once(url, timeout=http.TIMEOUT_S, headers=None):
        calls["n"] += 1
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(http, "fetch_once", fake_once)
    with pytest.raises(http.FriendlyError) as excinfo:
        run(http.fetch_raw("https://example.org/x", 1, 2))
    assert calls["n"] == 1
    assert "超时" in excinfo.value.message


def test_fetch_raw_network_error_retried(monkeypatch):
    monkeypatch.setattr(http, "_backoff", lambda attempt: 0)
    calls = {"n": 0}

    async def fake_once(url, timeout=http.TIMEOUT_S, headers=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("down")
        return httpx.Response(200, text="ok")

    monkeypatch.setattr(http, "fetch_once", fake_once)
    assert run(http.fetch_raw("https://example.org/x", 1, 2)) == "ok"
    assert calls["n"] == 3


def test_arxiv_gate_serializes_and_spaces_requests(monkeypatch):
    monkeypatch.setattr(http, "ARXIV_MIN_GAP_S", 0.05)
    stamps: list[float] = []

    async def fake_raw(url, timeout, retries):
        stamps.append(time.monotonic())
        return "ok"

    monkeypatch.setattr(http, "fetch_raw", fake_raw)

    async def main():
        return await asyncio.gather(
            http.arxiv_fetch("https://arxiv.org/api/1"),
            http.arxiv_fetch("https://arxiv.org/api/2"),
            http.arxiv_fetch("https://arxiv.org/api/3"),
        )

    assert run(main()) == ["ok", "ok", "ok"]
    assert len(stamps) == 3
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert all(gap >= 0.04 for gap in gaps), gaps


def test_safe_public_url_blocks_local_and_private():
    assert http.safe_public_url("https://arxiv.org/pdf/2401.00001")
    assert not http.safe_public_url("file:///etc/passwd")
    assert not http.safe_public_url("http://localhost:8010/x.pdf")
    assert not http.safe_public_url("http://127.0.0.1/x.pdf")
    assert not http.safe_public_url("http://192.168.1.2/x.pdf")
    assert not http.safe_public_url("http://10.0.0.5/x.pdf")
    assert not http.safe_public_url("http://172.20.0.1/x.pdf")
    assert not http.safe_public_url("http://169.254.1.1/x.pdf")


def test_proxy_helper_tolerates_ipv6_no_proxy(monkeypatch):
    """NO_PROXY 里带 [::1] 时 httpx 会抛 InvalidURL，所以代理要自己解析。"""
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,::1,[::1]")

    assert http._proxy_for("https://arxiv.org/api/query") == "http://127.0.0.1:7890"
    assert http._proxy_for("http://127.0.0.1:8010/api/health") is None

    client = http._client("https://arxiv.org/api/query")
    assert client.trust_env is False
    run(client.aclose())


def test_client_without_proxy_env(monkeypatch):
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(key, raising=False)
    client = http._client("https://arxiv.org/api/query")
    assert client.trust_env is False
    run(client.aclose())
    assert http._proxy_for("https://arxiv.org/api/query") is None


def test_looks_like_pdf_tolerates_leading_noise():
    assert http.looks_like_pdf(b"%PDF-1.7\n...")
    assert http.looks_like_pdf(b"\xef\xbb\xbf\n%PDF-1.4")
    assert not http.looks_like_pdf(b"<html><body>not a pdf</body></html>")
