"""引用出处（provenance）的单元测试。

覆盖 DOI / arXiv / 标题三条摘要抓取链路、无网络时的 DOI- arXiv 链接兜底、
上下文选句，以及 enrich 的结果缓存与逐级降级。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from unittest.mock import AsyncMock, patch

from paperead.tutor import provenance
from paperead.tutor.references import Reference


def make_ref(
    index: int = 1,
    raw: str = "",
    title: str = "A study of graph neural networks",
    year: int | None = 2016,
    authors: list[str] | None = None,
    suffix: str = "",
) -> Reference:
    return Reference(
        index=index,
        raw=raw,
        title=title,
        year=year,
        authors=list(authors or []),
        suffix=suffix,
    )


class _Resp:
    def __init__(self, status_code: int = 200, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"status {self.status_code}",
                request=httpx.Request("GET", "https://example.org"),
                response=httpx.Response(self.status_code),
            )


class _Client:
    """替身 AsyncClient：记录请求，按预设返回响应或抛异常。"""

    def __init__(self, resp: _Resp | None = None, exc: Exception | None = None):
        self._resp = resp
        self._exc = exc
        self.calls: list[tuple[str, dict | None]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url: str, params: dict | None = None):
        self.calls.append((url, params))
        if self._exc:
            raise self._exc
        assert self._resp is not None
        return self._resp


@pytest.fixture(autouse=True)
def clear_enrich_cache():
    provenance._cache.clear()
    yield
    provenance._cache.clear()


def _patch_client(client: _Client):
    return patch.object(provenance.httpx, "AsyncClient", lambda **kwargs: client)


# ── 链接兜底 ──────────────────────────────────────────


def test_link_for_prefers_doi_then_arxiv():
    assert provenance._link_for(make_ref(raw="Journal 12(3), doi:10.1000/abc.")) == (
        "https://doi.org/10.1000/abc",
        "doi",
    )
    assert provenance._link_for(make_ref(raw="arXiv:2401.12345")) == (
        "https://arxiv.org/abs/2401.12345",
        "arxiv",
    )
    assert provenance._link_for(make_ref(raw="No identifiers here.")) == ("", "none")


def test_ref_only_returns_entry_with_link():
    data = provenance.ref_only(
        make_ref(raw="doi:10.1000/abc.", title="Graph nets"), "Kipf 2016"
    )
    assert data["marker"] == "Kipf 2016"
    assert data["snippet"] == ""
    assert data["source"] == "doi"
    assert data["url"] == "https://doi.org/10.1000/abc"
    assert data["ref"]["title"] == "Graph nets"
    assert data["ref"]["authors"] == []


# ── 摘要字段解析 ──────────────────────────────────────


def test_abstract_from_inverted_orders_words_by_position():
    assert provenance._abstract_from_inverted(None) == ""
    assert provenance._abstract_from_inverted({}) == ""
    assert provenance._abstract_from_inverted({"world": [1], "hello": [0]}) == "hello world"

    # 同一词出现多次时按位置还原
    assert provenance._abstract_from_inverted({"a": [0, 2], "b": [1]}) == "a b a"


def test_openalex_by_doi_success_queries_doi_endpoint():
    client = _Client(_Resp(200, {"abstract_inverted_index": {"Deep": [0], "learning": [1]}}))
    with _patch_client(client):
        got = asyncio.run(provenance._openalex_by_doi("10.1000/abc"))
    assert got == ("Deep learning", "https://doi.org/10.1000/abc")
    assert client.calls[0][0].endswith("/works/https://doi.org/10.1000/abc")


def test_openalex_by_doi_returns_none_on_http_error_or_empty_abstract():
    bad = _Client(_Resp(404, {}))
    with _patch_client(bad):
        assert asyncio.run(provenance._openalex_by_doi("10.1/x")) is None

    empty = _Client(_Resp(200, {"abstract_inverted_index": {}}))
    with _patch_client(empty):
        assert asyncio.run(provenance._openalex_by_doi("10.1/x")) is None

    boom = _Client(exc=httpx.ConnectError("down"))
    with _patch_client(boom):
        assert asyncio.run(provenance._openalex_by_doi("10.1/x")) is None


def test_arxiv_abstract_parses_summary():
    client = _Client(_Resp(200, text="<entry><summary>A useful abstract.</summary></entry>"))
    with _patch_client(client):
        got = asyncio.run(provenance._arxiv_abstract("2401.00001"))
    assert got == ("A useful abstract.", "https://arxiv.org/abs/2401.00001")
    assert client.calls[0][0] == "http://export.arxiv.org/api/query"
    assert client.calls[0][1] == {"id_list": "2401.00001", "max_results": 1}


def test_arxiv_abstract_returns_none_without_summary_or_on_error():
    no_summary = _Client(_Resp(200, text="<entry></entry>"))
    with _patch_client(no_summary):
        assert asyncio.run(provenance._arxiv_abstract("2401.00001")) is None

    boom = _Client(exc=httpx.ReadTimeout("slow"))
    with _patch_client(boom):
        assert asyncio.run(provenance._arxiv_abstract("2401.00001")) is None


def test_openalex_by_title_skips_short_titles_without_network():
    client = _Client(_Resp(200, {"results": []}))
    with _patch_client(client):
        assert asyncio.run(provenance._openalex_by_title("Short")) is None
    assert client.calls == []


def test_openalex_by_title_accepts_close_match():
    payload = {
        "results": [
            {
                "title": "A study of graph neural networks",
                "primary_location": {"landing_page_url": "https://example.org/paper"},
                "abstract_inverted_index": {"Graph": [0], "nets": [1]},
            }
        ]
    }
    client = _Client(_Resp(200, payload))
    with _patch_client(client):
        got = asyncio.run(provenance._openalex_by_title("A study of graph neural networks"))
    assert got == ("Graph nets", "https://example.org/paper")
    assert client.calls[0][1]["search"] == "A study of graph neural networks"
    assert client.calls[0][1]["per_page"] == "3"


def test_openalex_by_title_rejects_weak_match_and_missing_abstract():
    weak = {
        "results": [
            {
                "title": "Completely different topic about databases",
                "primary_location": {"landing_page_url": "https://example.org/x"},
                "abstract_inverted_index": {"Data": [0]},
            }
        ]
    }
    client = _Client(_Resp(200, weak))
    with _patch_client(client):
        assert asyncio.run(provenance._openalex_by_title("A study of graph neural networks")) is None

    # 第一条没有标题（a/b 为空），第二条标题命中但没有摘要
    no_abstract = {
        "results": [
            {"primary_location": {"landing_page_url": "https://example.org/z"}},
            {
                "title": "A study of graph neural networks",
                "primary_location": {"landing_page_url": "https://example.org/y"},
            },
        ]
    }
    empty = _Client(_Resp(200, no_abstract))
    with _patch_client(empty):
        assert asyncio.run(provenance._openalex_by_title("A study of graph neural networks")) is None

    boom = _Client(exc=httpx.ConnectError("down"))
    with _patch_client(boom):
        assert asyncio.run(provenance._openalex_by_title("A study of graph neural networks")) is None


# ── 上下文选句 ────────────────────────────────────────


def test_best_sentence_prefers_context_overlap():
    abstract = (
        "Graph neural networks improve citation parsing in scholarly documents. "
        "Unrelated biology experiments describe protein folding mechanisms in detail."
    )
    picked = provenance._best_sentence(
        abstract, "citation parsing with graph neural networks", limit=60
    )
    assert picked.startswith("Graph neural networks")
    assert len(picked) <= 60


def test_best_sentence_falls_back_to_prefix_when_no_sentence_long_enough():
    assert provenance._best_sentence("Too short.", "any context") == "Too short."
    assert len(provenance._best_sentence("x" * 500, "ctx", limit=40)) == 40


def test_best_sentence_ignores_sentences_without_words():
    abstract = (
        "1111 1111 1111 1111 1111 1111 1111 1111. "
        "Graph networks improve citation parsing strongly."
    )
    picked = provenance._best_sentence(abstract, "graph networks")
    assert picked.startswith("Graph networks")


# ── enrich：逐级降级与缓存 ────────────────────────────


def test_enrich_uses_doi_first_and_caches_result():
    ref = make_ref(raw="doi:10.1000/abc.")
    client = _Client(_Resp(200, {"abstract_inverted_index": {"Citations": [0], "matter": [1]}}))
    with _patch_client(client):
        first = asyncio.run(provenance.enrich(ref, "citations matter for readers"))
        second = asyncio.run(provenance.enrich(ref, "citations matter for readers"))
    assert first["source"] == "openalex:doi"
    assert first["snippet"] == "Citations matter"
    assert first["marker"] == ""
    assert second == first
    # 第二次命中缓存，不再发请求
    assert len(client.calls) == 1


def test_enrich_falls_back_from_doi_to_arxiv():
    ref = make_ref(raw="doi:10.1000/abc. arXiv:2401.00001")
    with (
        patch.object(provenance, "_openalex_by_doi", AsyncMock(return_value=None)) as by_doi,
        patch.object(
            provenance,
            "_arxiv_abstract",
            AsyncMock(return_value=("Abstract text from arxiv.", "https://arxiv.org/abs/2401.00001")),
        ) as by_arxiv,
    ):
        data = asyncio.run(provenance.enrich(ref, "context"))
    by_doi.assert_awaited_once_with("10.1000/abc")
    by_arxiv.assert_awaited_once_with("2401.00001")
    assert data["source"] == "arxiv"
    assert data["snippet"] == "Abstract text from arxiv."
    assert data["url"] == "https://arxiv.org/abs/2401.00001"


def test_enrich_falls_back_to_title_search():
    ref = make_ref(raw="No ids here.", title="A study of graph neural networks")
    with patch.object(
        provenance,
        "_openalex_by_title",
        AsyncMock(return_value=("Graph nets abstract.", "https://example.org/p")),
    ) as by_title:
        data = asyncio.run(provenance.enrich(ref, "graph context"))
    by_title.assert_awaited_once_with("A study of graph neural networks")
    assert data["source"] == "openalex:title"
    assert data["url"] == "https://example.org/p"


def test_enrich_degrades_to_reference_link_when_abstracts_unavailable():
    ref = make_ref(raw="doi:10.1000/abc.")
    with (
        patch.object(provenance, "_openalex_by_doi", AsyncMock(return_value=None)),
        patch.object(provenance, "_openalex_by_title", AsyncMock(return_value=None)),
    ):
        data = asyncio.run(provenance.enrich(ref, "no snippet context"))
    assert data["snippet"] == ""
    assert data["url"] == "https://doi.org/10.1000/abc"
    assert data["source"] == "doi"


def test_enrich_keeps_none_source_without_identifiers():
    ref = make_ref(raw="Nothing to link.")
    with patch.object(provenance, "_openalex_by_title", AsyncMock(return_value=None)):
        data = asyncio.run(provenance.enrich(ref, "ctx"))
    assert data["url"] == ""
    assert data["source"] == "none"
    assert data["snippet"] == ""