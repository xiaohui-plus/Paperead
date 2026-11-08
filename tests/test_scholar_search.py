"""检索分派、详情、全文与中译英的测试（网络全部打桩）。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from paper_reflow.scholar import http
from paper_reflow.scholar import search as search_mod
from paper_reflow.scholar.http import FriendlyError
from paper_reflow.scholar.models import Paper
from paper_reflow.scholar.search import SearchInput, parse_google_translation

ARXIV_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
  <opensearch:totalResults>5</opensearch:totalResults>
  <entry>
    <id>http://arxiv.org/abs/2401.00001v1</id>
    <published>2024-01-02T10:00:00Z</published>
    <title>Graph Networks</title>
    <summary>We study graphs.</summary>
    <author><name>Ann Lee</name></author>
    <category term="cs.LG"/>
  </entry>
</feed>
"""


@pytest.fixture(autouse=True)
def empty_cache():
    http.cache.clear()
    yield
    http.cache.clear()


def run(coro):
    return asyncio.run(coro)


def test_search_arxiv_dispatch_and_total(monkeypatch):
    seen = {}

    async def fake_arxiv(url, **kwargs):
        seen["url"] = url
        return ARXIV_XML

    monkeypatch.setattr(search_mod, "arxiv_fetch", fake_arxiv)
    result, used, translated = run(search_mod.search(SearchInput(q="graph")))
    assert used == "graph"
    assert translated is False
    assert result.total == 5
    assert result.page_limit is None
    assert result.papers[0].id == "arxiv:2401.00001"
    assert "search_query=all:graph" in seen["url"]


def test_chinese_query_is_translated_before_search(monkeypatch):
    async def fake_fetch_text(url, **kwargs):
        return json.dumps([[["graph neural network", "图神经网络", None, None, 10]], None, "zh-CN"])

    seen = {}

    async def fake_arxiv(url, **kwargs):
        seen["url"] = url
        return ARXIV_XML

    monkeypatch.setattr(search_mod, "fetch_text", fake_fetch_text)
    monkeypatch.setattr(search_mod, "arxiv_fetch", fake_arxiv)

    result, used, translated = run(search_mod.search(SearchInput(q="图神经网络")))
    assert translated is True
    assert used == "graph neural network"
    assert "all:graph" in seen["url"]
    assert result.papers


def test_translation_failure_falls_back_to_original_query(monkeypatch):
    async def boom(url, **kwargs):
        raise FriendlyError("网络连接失败", True)

    seen = {}

    async def fake_arxiv(url, **kwargs):
        seen["url"] = url
        return ARXIV_XML

    monkeypatch.setattr(search_mod, "fetch_text", boom)
    monkeypatch.setattr(search_mod, "arxiv_fetch", fake_arxiv)

    _, used, translated = run(search_mod.search(SearchInput(q="图神经网络")))
    assert translated is False
    assert used == "图神经网络"
    assert seen["url"]


def test_openalex_search_uses_meta_count(monkeypatch):
    payload = {
        "meta": {"count": 99},
        "results": [
            {
                "id": "https://openalex.org/W1",
                "title": "Work",
                "cited_by_count": 3,
            }
        ],
    }

    async def fake_fetch_text(url, **kwargs):
        assert "search=" in url
        return json.dumps(payload)

    monkeypatch.setattr(search_mod, "fetch_text", fake_fetch_text)
    result, _, _ = run(search_mod.search(SearchInput(q="llm", source="openalex")))
    assert result.total == 99
    assert result.papers[0].id == "openalex:W1"


def test_europe_pmc_slices_window_and_reports_page_limit(monkeypatch):
    payload = {
        "hitCount": 500,
        "resultList": {
            "result": [
                {"id": f"{i}", "source": "MED", "title": f"T{i}"} for i in range(20)
            ]
        },
    }

    async def fake_fetch_text(url, **kwargs):
        return json.dumps(payload)

    monkeypatch.setattr(search_mod, "fetch_text", fake_fetch_text)
    result, _, _ = run(
        search_mod.search(SearchInput(q="crispr", source="europepmc", page=1, per_page=10))
    )
    assert result.page_limit == 20
    assert [p.title for p in result.papers] == [f"T{i}" for i in range(10, 20)]


def test_pubmed_search_fetches_esearch_then_esummary(monkeypatch):
    async def fake_fetch_text(url, **kwargs):
        if "esearch" in url:
            return json.dumps({"esearchresult": {"count": "2", "idlist": ["11", "22"]}})
        return json.dumps(
            {
                "result": {
                    "11": {"uid": "11", "title": "First", "pubdate": "2020"},
                    "22": {"uid": "22", "title": "Second", "pubdate": "2021"},
                }
            }
        )

    monkeypatch.setattr(search_mod, "fetch_text", fake_fetch_text)
    result, _, _ = run(search_mod.search(SearchInput(q="cancer", source="pubmed")))
    assert result.total == 2
    assert [p.id for p in result.papers] == ["pubmed:11", "pubmed:22"]


def test_search_empty_query_raises_friendly():
    with pytest.raises(FriendlyError):
        run(search_mod.search(SearchInput(q="   ")))


def test_search_input_clean_clamps_values():
    cleaned = SearchInput(
        q="  x  ", source="nope", sort="bad", field="bad", page=999, per_page=1,
        category="  cs.LG  ", from_year=900, to_year=2024,
    ).clean()
    assert cleaned.q == "x"
    assert cleaned.source == "arxiv"
    assert cleaned.sort == "rel"
    assert cleaned.field == "all"
    assert cleaned.page == 50
    assert cleaned.per_page == 10
    assert cleaned.category == "cs.LG"
    assert cleaned.from_year is None
    assert cleaned.to_year == 2024


def test_get_paper_arxiv_detail(monkeypatch):
    async def fake_arxiv(url, **kwargs):
        return ARXIV_XML

    async def no_html(raw_id):
        return False

    monkeypatch.setattr(search_mod, "arxiv_fetch", fake_arxiv)
    monkeypatch.setattr(search_mod, "has_arxiv_html", no_html)
    paper = run(search_mod.get_paper("arxiv:2401.00001"))
    assert paper is not None
    assert paper.title == "Graph Networks"
    assert paper.html_url is None


def test_get_paper_unknown_or_malformed_returns_none():
    assert run(search_mod.get_paper("not-an-id")) is None
    assert run(search_mod.get_paper("unknown:123")) is None


def test_find_free_full_text_404_is_not_an_error(monkeypatch):
    async def fake_once(url, timeout=12):
        return httpx.Response(404)

    monkeypatch.setattr(search_mod, "fetch_once", fake_once)
    result = run(search_mod.find_free_full_text("10.1/missing"))
    assert result["pdf_url"] is None
    assert result["pdf_urls"] == []
    assert result["error"] is None


def test_find_free_full_text_reads_url_for_pdf(monkeypatch):
    async def fake_once(url, timeout=12):
        return httpx.Response(
            200,
            json={
                "oa_status": "gold",
                "best_oa_location": {
                    "url_for_pdf": "https://x/pdf",
                    "url": "https://x/landing",
                },
                "oa_locations": [
                    {"url_for_pdf": "https://x/pdf", "host_type": "publisher"},
                    {"url_for_pdf": "https://pmc/pdf", "host_type": "repository"},
                    {"url_for_landing_page": "https://x/html"},
                ],
            },
        )

    monkeypatch.setattr(search_mod, "fetch_once", fake_once)
    result = run(search_mod.find_free_full_text("https://doi.org/10.1/x"))
    assert result["pdf_url"] == "https://x/pdf"
    assert result["landing_url"] == "https://x/landing"
    assert result["oa_status"] == "gold"
    # 出版商挡爬虫时可以换 PMC 镜像，所以候选要按顺序留着
    assert result["pdf_urls"] == ["https://x/pdf", "https://pmc/pdf"]


def test_resolve_pdf_urls_prefers_record_link():
    paper = Paper(id="arxiv:1", source="arxiv", title="T", pdf_url="https://x/a.pdf")
    assert run(search_mod.resolve_pdf_urls(paper)) == ["https://x/a.pdf"]


def test_resolve_pdf_urls_appends_unpaywall_candidates(monkeypatch):
    async def fake_lookup(doi):
        return {
            "pdf_url": "https://pub/pdf",
            "pdf_urls": ["https://pub/pdf", "https://pmc/pdf"],
            "landing_url": None,
            "oa_status": "green",
            "error": None,
        }

    monkeypatch.setattr(search_mod, "find_free_full_text", fake_lookup)
    paper = Paper(id="openalex:W1", source="openalex", title="T", doi="10.1/x")
    assert run(search_mod.resolve_pdf_urls(paper)) == ["https://pub/pdf", "https://pmc/pdf"]


def test_resolve_pdf_urls_empty_without_doi():
    paper = Paper(id="arxiv:1", source="arxiv", title="T")
    assert run(search_mod.resolve_pdf_urls(paper)) == []


def test_fetch_pdf_rejects_non_pdf(monkeypatch):
    async def fake_bytes(url, **kwargs):
        return b"<html>login</html>"

    monkeypatch.setattr(search_mod, "fetch_bytes", fake_bytes)
    with pytest.raises(FriendlyError) as excinfo:
        run(search_mod.fetch_pdf("https://example.org/a.pdf"))
    assert "不是 PDF" in excinfo.value.message


def test_fetch_pdf_rejects_private_url():
    with pytest.raises(FriendlyError):
        run(search_mod.fetch_pdf("http://127.0.0.1:8010/secret.pdf"))


def test_pdf_filename_is_safe():
    paper = Paper(id="arxiv:2401.00001", source="arxiv", title="Graph / Neural: Networks 2024")
    name = search_mod.pdf_filename(paper)
    assert name.endswith("-2401.00001.pdf")
    assert "/" not in name and ":" not in name


def test_parse_google_translation_both_shapes():
    assert parse_google_translation([["Hello", "zh-CN"]]) == "Hello"
    assert parse_google_translation([[["Hello", "你好", None, None, 10]], None, "zh-CN"]) == "Hello"
    assert parse_google_translation([]) == ""
    assert parse_google_translation("nope") == ""


def test_has_cjk():
    assert search_mod.has_cjk("图神经网络")
    assert search_mod.has_cjk("グラフ")
    assert not search_mod.has_cjk("graph neural network")
