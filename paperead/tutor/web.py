"""网络检索：Tavily（可选 Key）→ 360 搜索 → Bing → OpenAlex 学术补充。

设计要点：
- 用户不一定申请 Tavily Key，所以默认必须有一条不需要任何配置的可用路径；
- 实测国内网络下 Bing 对中文长查询会被降级成单字词典结果，360 搜索（so.com）
  对中英文查询都能返回相关结果且直接给出真实 URL，因此作为免密钥主源；
- 搜索结果按 (query, limit) 做短时缓存，重复提问不会重复打网络；
- 任何一路失败都只是回退到下一路，最坏返回空列表，不阻塞论文内问答。
"""

from __future__ import annotations

import asyncio
import base64
import html as html_lib
import ipaddress
import logging
import os
import re
import time
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlparse

import httpx

log = logging.getLogger(__name__)

TAVILY_URL = "https://api.tavily.com/search"
SO360_URL = "https://www.so.com/s"
BING_URLS = ("https://cn.bing.com/search", "https://www.bing.com/search")
DDG_URL = "https://html.duckduckgo.com/html/"
OPENALEX_URL = "https://api.openalex.org/works"
# OpenAlex 按邮箱分配配额，带上 mailto 更不容易被限流
_MAILTO = os.getenv("PAPEREAD_MAILTO", "paperead@users.noreply.github.com")
_academic_block_until = 0.0

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

_CACHE_TTL = 600
# 搜不到结果的缓存时间要短：网络抖动一次不该让后续十分钟都拿空结果
_EMPTY_CACHE_TTL = 60
_CACHE_MAX = 120
_cache: dict[tuple[str, int], tuple[float, list[dict]]] = {}

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(?:script|style)\b.*?</(?:script|style)>", re.S | re.I)
_WS_RE = re.compile(r"\s+")
_EN_TERM_RE = re.compile(r"[A-Za-z][A-Za-z\-]{3,}")


def has_key() -> bool:
    """Tavily 是否配置了 Key（不是联网的前置条件）。"""
    return bool(os.getenv("TAVILY_API_KEY"))


def _mode() -> str:
    """搜索后端偏好：auto（默认）/ tavily / bing / ddg / off。"""
    return (os.getenv("PAPEREAD_WEB_SEARCH") or "auto").strip().lower()


def status() -> dict:
    return {
        "tavily": has_key(),
        "mode": _mode(),
        "engines": ["tavily", "so360", "bing", "duckduckgo", "openalex"],
    }


def _has_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


def filter_relevant(query: str, hits: list[dict]) -> list[dict]:
    """英文 query 下拉回 Bing 时，剔除把术语拆成单词后的词典/无关结果。

    例如 "transformer attention mechanism" 会被降级成 "self 的用法" 这类结果；
    标题与摘要都不含查询里任一长术语的直接丢弃，宁可无结果也不污染回答。
    """
    terms = [t.lower() for t in _EN_TERM_RE.findall(query or "")]
    if not terms:
        return hits
    out: list[dict] = []
    for hit in hits:
        text = f"{hit.get('title', '')} {hit.get('content', '')}".lower()
        if any(term in text for term in terms):
            out.append(hit)
    return out


def _clean(fragment: str) -> str:
    text = _SCRIPT_RE.sub(" ", fragment or "")
    text = html_lib.unescape(_TAG_RE.sub(" ", text))
    return _WS_RE.sub(" ", text).strip()


def _unwrap_bing_url(href: str) -> str:
    """Bing 有时把结果包成 /ck/a?...&u=a1<base64url>，需要还原真实地址。"""
    href = (href or "").strip()
    if not href.startswith("http") or "bing.com/ck/a" not in href:
        return href
    raw = (parse_qs(urlparse(href).query).get("u") or [""])[0]
    if raw.startswith("a1"):
        body = raw[2:]
        try:
            return base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode(
                "utf-8", "ignore"
            )
        except Exception:  # noqa: BLE001
            return href
    return href


def _unwrap_ddg_url(href: str) -> str:
    href = (href or "").strip()
    if href.startswith("//"):
        href = "https:" + href
    if "duckduckgo.com/l/" not in href:
        return href
    target = (parse_qs(urlparse(href).query).get("uddg") or [""])[0]
    return unquote(target) or href


def parse_360(html: str, limit: int = 4) -> list[dict]:
    """360 搜索：结果块带 data-mdurl 属性，能直接拿到真实地址，不用跟随跳转。"""
    out: list[dict] = []
    chunks = re.split(r'(?=<li class="res-list)', html or "")
    for block in chunks[1:]:
        head = re.search(r'<h3[^>]*class="res-title[^"]*"[^>]*>(.*?)</h3>', block, re.S)
        if not head:
            continue
        link = re.search(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', head.group(1), re.S)
        if not link:
            continue
        mdurl = re.search(r'data-mdurl="([^"]+)"', link.group(0))
        url = _clean(mdurl.group(1)) if mdurl else html_lib.unescape(link.group(1))
        title = _clean(link.group(2))
        if not title or not url.startswith("http"):
            continue
        # 站内 AI 卡片/跳转链接：没有真实站点，不当作网络来源
        if (urlparse(url).hostname or "").endswith("so.com"):
            continue
        desc = re.search(
            r'<span[^>]*data-result-summary[^>]*>(.*?)</span>', block, re.S
        )
        if not desc:
            desc = re.search(r'class="res-desc[^"]*"[^>]*>(.*?)</p>', block, re.S)
        out.append(
            {
                "title": title[:160],
                "url": url,
                "content": _clean(desc.group(1))[:600] if desc else "",
                "engine": "so360",
            }
        )
        if len(out) >= limit:
            break
    return out


def parse_bing(html: str, limit: int = 4) -> list[dict]:
    out: list[dict] = []
    for block in re.findall(r'<li class="b_algo".*?</li>', html or "", re.S):
        head = re.search(r"<h2[^>]*>(.*?)</h2>", block, re.S)
        if not head:
            continue
        link = re.search(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', head.group(1), re.S)
        if not link:
            continue
        title = _clean(link.group(2))
        url = _unwrap_bing_url(html_lib.unescape(link.group(1)))
        if not title or not url.startswith("http"):
            continue
        para = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        snippet = _clean(para.group(1)) if para else ""
        out.append(
            {
                "title": title[:160],
                "url": url,
                "content": snippet[:600],
                "engine": "bing",
            }
        )
        if len(out) >= limit:
            break
    return out


def parse_ddg(html: str, limit: int = 4) -> list[dict]:
    out: list[dict] = []
    for block in re.findall(r'<div class="result[^"]*".*?</div>\s*</div>', html or "", re.S):
        link = re.search(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not link:
            continue
        url = _unwrap_ddg_url(html_lib.unescape(link.group(1)))
        title = _clean(link.group(2))
        if not title or not url.startswith("http"):
            continue
        snip = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', block, re.S)
        out.append(
            {
                "title": title[:160],
                "url": url,
                "content": _clean(snip.group(1))[:600] if snip else "",
                "engine": "duckduckgo",
            }
        )
        if len(out) >= limit:
            break
    return out


def _reconstruct_abstract(index: dict | None) -> str:
    if not isinstance(index, dict):
        return ""
    slots: dict[int, str] = {}
    for word, positions in index.items():
        for pos in positions or []:
            if isinstance(pos, int):
                slots[pos] = word
    return " ".join(slots[i] for i in sorted(slots))


def parse_openalex(payload: dict, limit: int = 3) -> list[dict]:
    out: list[dict] = []
    for item in (payload or {}).get("results") or []:
        title = (item.get("display_name") or item.get("title") or "").strip()
        location = item.get("primary_location") or {}
        url = (
            location.get("landing_page_url")
            or item.get("doi")
            or item.get("id")
            or ""
        )
        if not title or not url:
            continue
        year = item.get("publication_year")
        abstract = _reconstruct_abstract(item.get("abstract_inverted_index"))
        prefix = f"（{year}）" if year else ""
        out.append(
            {
                "title": f"{prefix}{title}"[:160],
                "url": url,
                "content": abstract[:600],
                "engine": "openalex",
            }
        )
        if len(out) >= limit:
            break
    return out


async def _get(url: str, params: dict, timeout: float) -> httpx.Response:
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=True, headers=_HEADERS
    ) as c:
        return await c.get(url, params=params)


async def _tavily(query: str, limit: int) -> list[dict]:
    key = os.getenv("TAVILY_API_KEY")
    if not key:
        return []
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                TAVILY_URL,
                json={
                    "api_key": key,
                    "query": query,
                    "max_results": limit,
                    "search_depth": "basic",
                    "include_answer": False,
                },
            )
            r.raise_for_status()
            data = r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("Tavily 搜索失败：%s", e)
        return []
    out = []
    for item in data.get("results") or []:
        out.append(
            {
                "title": (item.get("title") or "").strip()[:160],
                "url": item.get("url") or "",
                "content": (item.get("content") or "").strip()[:600],
                "engine": "tavily",
            }
        )
    return [x for x in out if x["url"]][:limit]


async def _so360(query: str, limit: int) -> list[dict]:
    try:
        r = await _get(SO360_URL, {"q": query}, 10)
        if r.status_code != 200:
            return []
        return parse_360(r.text, limit)
    except Exception as e:  # noqa: BLE001
        log.info("360 搜索失败：%s", e)
        return []


async def _bing(query: str, limit: int) -> list[dict]:
    for url in BING_URLS:
        try:
            r = await _get(url, {"q": query, "setlang": "zh-Hans"}, 10)
            if r.status_code != 200:
                continue
            hits = filter_relevant(query, parse_bing(r.text, limit))
            if hits:
                return hits
        except Exception as e:  # noqa: BLE001
            log.info("Bing(%s) 失败：%s", url, e)
    return []


async def _ddg(query: str, limit: int) -> list[dict]:
    try:
        async with httpx.AsyncClient(
            timeout=8, follow_redirects=True, headers=_HEADERS
        ) as c:
            r = await c.post(DDG_URL, data={"q": query})
            if r.status_code != 200:
                return []
            return parse_ddg(r.text, limit)
    except Exception as e:  # noqa: BLE001
        log.info("DuckDuckGo 失败：%s", e)
        return []


async def search_academic(query: str, limit: int = 3) -> list[dict]:
    """学术补充：OpenAlex 免 Key，用于概念/相关工作类问题。

    该接口偶发限流，失败时短时间内不再重试，避免拖慢每一次问答。
    """
    global _academic_block_until
    if time.time() < _academic_block_until:
        return []
    for attempt in range(2):
        try:
            r = await _get(
                OPENALEX_URL,
                {
                    "search": query,
                    "per-page": max(1, min(limit, 5)),
                    "mailto": _MAILTO,
                },
                12,
            )
            if r.status_code == 200:
                return parse_openalex(r.json(), limit)
            log.info("OpenAlex HTTP %s", r.status_code)
            if r.status_code in (429, 403):
                _academic_block_until = time.time() + 120
                return []
        except Exception as e:  # noqa: BLE001
            log.info("OpenAlex 搜索失败（第 %s 次）：%s", attempt + 1, e)
        await asyncio.sleep(0.5)
    return []


def _cache_get(key: tuple[str, int]) -> list[dict] | None:
    item = _cache.get(key)
    if not item:
        return None
    stamp, results = item
    ttl = _CACHE_TTL if results else _EMPTY_CACHE_TTL
    if time.time() - stamp > ttl:
        _cache.pop(key, None)
        return None
    return [dict(x) for x in results]


def _cache_put(key: tuple[str, int], results: list[dict]) -> None:
    if len(_cache) >= _CACHE_MAX:
        oldest = min(_cache, key=lambda k: _cache[k][0])
        _cache.pop(oldest, None)
    _cache[key] = (time.time(), [dict(x) for x in results])


async def search(query: str, limit: int = 4) -> list[dict]:
    """通用网页搜索：不需要任何 Key 也能出结果。"""
    query = (query or "").strip()
    if not query or _mode() == "off":
        return []
    key = (query.lower(), limit)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    mode = _mode()
    has_cjk = _has_cjk(query)
    results: list[dict] = []
    if mode in ("auto", "tavily"):
        results = await _tavily(query, limit)
    if not results and mode in ("auto", "so360"):
        results = await _so360(query, limit)
    # 实测 Bing 对中文长查询会降级成单字词典结果，宁可回退 DDG 也不要污染材料
    if not results and mode in ("auto", "bing") and not has_cjk:
        results = await _bing(query, limit)
    if not results and mode in ("auto", "ddg", "bing"):
        results = await _ddg(query, limit)

    _cache_put(key, results)
    if not results:
        log.info("网络检索无结果：%s", query[:80])
    return results


def dedupe(items: list[dict]) -> list[dict]:
    """按 URL 去重，保留先出现的一条（Tavily/Bing 在前，学术补充在后）。"""
    out: list[dict] = []
    seen: set[str] = set()
    for item in items:
        url = (item.get("url") or "").strip().rstrip("/")
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(item)
    return out


# ── web_fetch：把网页正文取回来，供模型读细节 ───────────────────

_SKIP_TAGS = {
    "script", "style", "nav", "header", "footer", "aside", "form",
    "iframe", "svg", "noscript", "button",
}
_BLOCK_TAGS = {
    "p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "br", "pre", "blockquote", "tr", "section", "article", "table",
}


class _TextExtractor(HTMLParser):
    """极简正文抽取：去掉脚本/导航/页脚，保留块级换行。

    不追求通用正文识别（那需要 readability 类库），目标只是给模型一段
    比搜索摘要更完整、噪声可接受的页面文字。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.parts: list[str] = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self.skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skip:
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        lines = [re.sub(r"[ \t\u3000]+", " ", line).strip() for line in raw.splitlines()]
        kept = [line for line in lines if line]
        return "\n\n".join(kept)


def _is_public_http_url(url: str) -> bool:
    """只允许公网 http(s)：防止模型被诱导去抓内网/本机地址。"""
    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if not host or host in ("localhost", "0.0.0.0") or host.endswith(".local"):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
    )


async def fetch(url: str, max_chars: int = 2400) -> dict:
    """抓取网页正文；失败时返回带 error 的结果，由调用方退回到搜索摘要。"""
    url = (url or "").strip()
    if not _is_public_http_url(url):
        return {"url": url, "statusCode": 0, "content": "", "truncated": False, "title": "", "error": "只允许抓取公网 http(s) 地址"}
    try:
        async with httpx.AsyncClient(
            timeout=12, follow_redirects=True, headers=_HEADERS
        ) as c:
            r = await c.get(url)
    except Exception as e:  # noqa: BLE001
        log.info("网页正文抓取失败 %s：%s", url, e)
        return {"url": url, "statusCode": 0, "content": "", "truncated": False, "title": "", "error": f"抓取失败：{type(e).__name__}"}

    ctype = (r.headers.get("content-type") or "").lower()
    if r.status_code >= 400 or not r.text:
        return {
            "url": url,
            "statusCode": r.status_code,
            "content": "",
            "truncated": False,
            "title": "",
            "error": f"HTTP {r.status_code}",
        }

    if "html" in ctype or r.text.lstrip().startswith("<"):
        parser = _TextExtractor()
        try:
            parser.feed(r.text)
            parser.close()
        except Exception:  # noqa: BLE001
            pass
        body = parser.text()
        title = _WS_RE.sub(" ", html_lib.unescape(parser.title)).strip()[:160]
    else:
        body = _WS_RE.sub(" ", r.text).strip()
        title = ""

    if not body:
        return {"url": url, "statusCode": r.status_code, "content": "", "truncated": False, "title": title, "error": "页面没有可提取的正文"}
    return {
        "url": url,
        "statusCode": r.status_code,
        "content": body[:max_chars],
        "truncated": len(body) > max_chars,
        "title": title,
        "error": "",
    }


async def search_many(
    queries: list[str],
    limit: int = 4,
    total_limit: int = 6,
    academic: bool = False,
    academic_query: str = "",
) -> list[dict]:
    """多路查询并发取回并去重；queries 通常是「中文原问」+「英文关键词」。"""
    uniq: list[str] = []
    for q in queries:
        q = (q or "").strip()
        if q and q.lower() not in {x.lower() for x in uniq}:
            uniq.append(q)
    uniq = uniq[:3]
    if not uniq:
        return []

    jobs = [search(q, limit) for q in uniq]
    if academic and academic_query.strip():
        jobs.append(search_academic(academic_query.strip(), 3))

    batches = await asyncio.gather(*jobs, return_exceptions=True)
    buckets: list[list[dict]] = []
    for batch in batches:
        if isinstance(batch, Exception):
            log.info("检索分支失败：%s", batch)
            continue
        if batch:
            buckets.append(list(batch))

    # 轮转合并：每个来源都能进前几名，避免学术补充被通用结果的尾部截断
    merged: list[dict] = []
    depth = max((len(b) for b in buckets), default=0)
    for i in range(depth):
        for bucket in buckets:
            if i < len(bucket):
                merged.append(bucket[i])
    return dedupe(merged)[:total_limit]