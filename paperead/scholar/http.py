"""外部学术接口的网络层：缓存、并发合并、超时重试、arXiv 节流。

外部服务都很脆：
- arXiv 官方要求「每 3 秒最多 1 个请求，且只保持单连接」；
- OpenAlex / Unpaywall 有礼貌池（mailto），但仍会偶发 429；
- PubMed 的 esearch 之后还要 esummary / efetch，同一个查询会被前端重复触发。
所以这一层做三件事：按 TTL 缓存、把同 key 的并发请求合并成一次、失败退避重试并换成人话错误。
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable

import httpx

from paperead.env import env

log = logging.getLogger(__name__)

# arXiv 官方要求 User-Agent 带上应用名与联系方式。
# Unpaywall 会拒绝 example.com 这类占位邮箱（422），默认用一个可用的项目地址；
# 部署方可以通过 PAPEREAD_MAILTO 换成自己的邮箱，走礼貌池也更稳。
CONTACT_EMAIL = env("PAPEREAD_MAILTO", "paperead@users.noreply.github.com")
USER_AGENT = f"Paperead/0.1 (mailto:{CONTACT_EMAIL})"
_HEADERS = {"User-Agent": USER_AGENT, "Accept": "*/*"}

TIMEOUT_S = 12.0
MAX_RETRIES = 2
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# 不同资源的新鲜度差别极大，按用途分别设 TTL。
TTL_SEARCH = 5 * 60
TTL_DETAIL = 6 * 60 * 60
TTL_TRANSLATE = 24 * 60 * 60
TTL_UNPAYWALL = 6 * 60 * 60
TTL_PROBE = 60 * 60

# arXiv 官方限流：legacy API 每 3 秒最多 1 个请求。
ARXIV_MIN_GAP_S = 3.0

MAX_DOWNLOAD_BYTES = 60 * 1024 * 1024


class FriendlyError(Exception):
    """可直接展示给用户的错误。`retryable` 决定要不要退避重试。"""

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.message = message
        self.retryable = retryable


def friendly(exc: BaseException) -> FriendlyError:
    if isinstance(exc, FriendlyError):
        return exc
    if isinstance(exc, httpx.TimeoutException):
        return FriendlyError("检索请求超时，请检查网络后重试。", False)
    return FriendlyError("网络连接失败，请检查网络后重试。", True)


def _backoff(attempt: int) -> float:
    """指数退避 + 抖动，避免所有重试同时到达。"""
    return 0.4 * (2 ** (attempt - 1)) + random.random() * 0.25


class RequestCache:
    """进程内 TTL 缓存 + 同 key 在途合并（LRU 上限）。"""

    def __init__(self, max_entries: int = 400):
        self.max_entries = max_entries
        self._data: OrderedDict[str, tuple[float, float, str]] = OrderedDict()
        self._inflight: dict[str, asyncio.Future[str]] = {}

    def __len__(self) -> int:
        return len(self._data)

    def clear(self) -> None:
        self._data.clear()
        self._inflight.clear()

    def _read(self, key: str) -> str | None:
        hit = self._data.get(key)
        if hit is None:
            return None
        at, ttl, text = hit
        if time.monotonic() - at > ttl:
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return text

    def _write(self, key: str, text: str, ttl: float) -> None:
        if len(self._data) >= self.max_entries:
            now = time.monotonic()
            for k in [k for k, (at, t, _) in self._data.items() if now - at > t]:
                self._data.pop(k, None)
            while len(self._data) >= self.max_entries and self._data:
                self._data.popitem(last=False)
        self._data[key] = (time.monotonic(), ttl, text)

    async def get(
        self, key: str, ttl: float, run: Callable[[], Awaitable[str]]
    ) -> str:
        hit = self._read(key)
        if hit is not None:
            return hit

        pending = self._inflight.get(key)
        if pending is not None:
            return await pending

        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._inflight[key] = future
        try:
            text = await run()
        except BaseException as exc:
            self._inflight.pop(key, None)
            if not future.done():
                future.set_exception(exc)
            # 标记异常已取回，避免 asyncio「never retrieved」告警；真正的错误仍往上抛。
            future.exception()
            raise
        self._inflight.pop(key, None)
        self._write(key, text, ttl)
        if not future.done():
            future.set_result(text)
        return text


# 全局共享实例：所有数据源共用一套缓存与在途合并表。
cache = RequestCache()


def _client(url: str, timeout: float | None = None) -> httpx.AsyncClient:
    """按目标地址决定要不要走代理。

    不能直接依赖 httpx 的 `trust_env`：`NO_PROXY` 里出现 `[::1]` / `::1` 这类
    IPv6 写法时，httpx 0.28 会在构造 URLPattern 时抛 `InvalidURL`，
    结果连 arXiv 都请求不出去（很多代理软件的默认配置就长这样）。
    这里自己解析代理环境变量，并用 urllib 的判断处理 no_proxy。
    """
    return httpx.AsyncClient(
        timeout=timeout or TIMEOUT_S,
        headers=_HEADERS,
        follow_redirects=True,
        proxy=_proxy_for(url),
        trust_env=False,
    )


def _proxy_for(url: str) -> str | None:
    from urllib.parse import urlparse
    from urllib.request import getproxies_environment, proxy_bypass_environment

    try:
        proxies = getproxies_environment()
    except Exception:
        return None
    parsed = urlparse(url)
    host = parsed.hostname or ""
    try:
        if proxies.get("no") and proxy_bypass_environment(host, proxies):
            return None
    except Exception:
        pass
    return proxies.get(parsed.scheme or "https") or proxies.get("all")


async def fetch_once(url: str, timeout: float = TIMEOUT_S) -> httpx.Response:
    async with _client(url, timeout) as client:
        return await client.get(url)


async def fetch_raw(url: str, timeout: float, retries: int) -> str:
    """纯网络请求：超时 + 重试退避，不碰缓存。"""
    last: FriendlyError | None = None
    for attempt in range(retries + 1):
        if attempt:
            await asyncio.sleep(_backoff(attempt))
        try:
            response = await fetch_once(url, timeout)
        except httpx.TimeoutException:
            # 超时不重试，避免用户等待过久
            raise FriendlyError("检索请求超时，请检查网络后重试。", False) from None
        except httpx.HTTPError:
            last = FriendlyError("网络连接失败，请检查网络后重试。", True)
            continue

        if response.status_code < 400:
            return response.text
        if response.status_code == 404:
            raise FriendlyError("没有找到这条记录。", False)
        retryable = response.status_code in RETRYABLE_STATUS
        message = (
            "请求过于频繁，检索服务正在限流，请稍后重试。"
            if response.status_code == 429
            else f"检索服务暂时不可用（{response.status_code}），请稍后重试。"
        )
        if not retryable:
            raise FriendlyError(message, False)
        last = FriendlyError(message, True)

    raise last or FriendlyError("检索服务暂时不可用，请稍后重试。", True)


async def fetch_text(
    url: str,
    *,
    timeout: float = TIMEOUT_S,
    retries: int = MAX_RETRIES,
    ttl: float = TTL_SEARCH,
) -> str:
    return await cache.get(url, ttl, lambda: fetch_raw(url, timeout, retries))


# ── arXiv 节流闸门 ──────────────────────────────────────────
# 把请求串成一条链：同一时刻只有一个在飞，两次请求间隔不少于 ARXIV_MIN_GAP_S。
# 缓存命中或在途合并都在 fetch_text 内完成，不占用节流窗口。
_gate_lock: asyncio.Lock | None = None
_gate_loop: object | None = None
_gate_last = 0.0


def _gate() -> asyncio.Lock:
    """按事件循环懒创建锁（测试里会换循环，锁不能跨循环复用）。"""
    global _gate_lock, _gate_loop
    loop = asyncio.get_running_loop()
    if _gate_loop is not loop:
        _gate_lock, _gate_loop = asyncio.Lock(), loop
    return _gate_lock


async def with_arxiv_gate(run: Callable[[], Awaitable[Any]]) -> Any:
    """把一次 arXiv 外呼放进节流闸门：串行 + 最小间隔。"""
    global _gate_last
    async with _gate():
        loop = asyncio.get_running_loop()
        wait = ARXIV_MIN_GAP_S - (loop.time() - _gate_last)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            return await run()
        finally:
            _gate_last = asyncio.get_running_loop().time()


async def arxiv_fetch(
    url: str,
    *,
    timeout: float = TIMEOUT_S,
    retries: int = MAX_RETRIES,
    ttl: float = TTL_SEARCH,
) -> str:
    return await cache.get(
        url, ttl, lambda: with_arxiv_gate(lambda: fetch_raw(url, timeout, retries))
    )


async def fetch_bytes(
    url: str, *, timeout: float = 60.0, max_bytes: int = MAX_DOWNLOAD_BYTES
) -> bytes:
    """下载二进制（PDF），边读边限长，避免被超大文件拖死。"""
    try:
        async with _client(url, timeout) as client:
            async with client.stream(
                "GET", url, headers={"Accept": "application/pdf,application/octet-stream,*/*"}
            ) as response:
                if response.status_code >= 400:
                    raise FriendlyError(
                        f"全文下载失败（{response.status_code}），请稍后重试。",
                        response.status_code in RETRYABLE_STATUS,
                    )
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise FriendlyError("全文超过 60 MB，请到原文页面手动下载。", False)
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes(64 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise FriendlyError("全文超过 60 MB，请到原文页面手动下载。", False)
                    chunks.append(chunk)
                return b"".join(chunks)
    except httpx.TimeoutException:
        raise FriendlyError("全文下载超时，请稍后重试。", True) from None
    except httpx.HTTPError as exc:
        raise FriendlyError(f"全文下载失败：{exc}", True) from None


_PDF_MAGIC = re.compile(rb"%PDF-")


def looks_like_pdf(data: bytes) -> bool:
    """PDF 头允许出现在文件前 1 KB（有些服务器会加 BOM/空行）。"""
    return bool(_PDF_MAGIC.search(data[:1024]))


def safe_public_url(url: str) -> bool:
    """下载白名单：只允许 http(s)，挡掉本机/内网地址，避免被题录里的链接带偏。"""
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"} or host.endswith(".local"):
        return False
    if re.match(r"^(10|127)\.", host) or re.match(r"^192\.168\.", host):
        return False
    if re.match(r"^172\.(1[6-9]|2[0-9]|3[01])\.", host):
        return False
    if re.match(r"^169\.254\.", host):
        return False
    return True
