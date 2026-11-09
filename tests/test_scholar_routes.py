"""HTTP 路由测试：用假 store / 假 PDF，不联网。"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from paperead.scholar import search as search_mod
from paperead.scholar.http import FriendlyError
from paperead.scholar.models import Paper
from paperead.scholar.search import SearchResult
from server import scholar_api
from server.scholar_api import build_router


class FakeStore:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.by_source: dict[str, dict] = {}

    def find_by_source(self, source_id: str):
        return self.by_source.get(source_id)


class FakeLibrary:
    def __init__(self) -> None:
        self.store = FakeStore()
        self.added: list[tuple[str, bytes, dict]] = []

    def add_pdf(self, filename: str, data: bytes, meta: dict) -> dict:
        row = {
            "id": f"p_{len(self.added) + 1:03d}",
            "filename": filename,
            "title": meta.get("title") or filename,
            "status": "parsing",
        }
        self.added.append((filename, data, meta))
        self.store.rows.append(row)
        if meta.get("source_id"):
            self.store.by_source[meta["source_id"]] = row
        return row

    def public(self, row: dict) -> dict:
        return {**row, "file_url": f"/api/papers/{row['id']}/file"}

    def app(self) -> FastAPI:
        app = FastAPI()
        app.include_router(build_router(self.store, self.add_pdf, self.public))
        return app


@pytest.fixture()
def library() -> FakeLibrary:
    return FakeLibrary()


@pytest.fixture()
def client(library: FakeLibrary):
    with TestClient(library.app()) as c:
        yield c


def paper(**overrides) -> Paper:
    base = dict(
        id="arxiv:2401.00001",
        source="arxiv",
        title="Graph Networks",
        authors=["Ann Lee"],
        venue="arXiv 预印本",
        year=2024,
        category="cs.LG",
        abstract="We study graphs.",
        url="https://arxiv.org/abs/2401.00001",
        pdf_url="https://arxiv.org/pdf/2401.00001",
        doi=None,
    )
    base.update(overrides)
    return Paper(**base)


def test_search_success_shape(client, monkeypatch):
    async def fake_search(data):
        assert data.q == "graph"
        return SearchResult([paper()], 5, None), "graph", False

    monkeypatch.setattr(search_mod, "search", fake_search)
    res = client.get("/api/scholar/search", params={"q": "graph"})
    body = res.json()
    assert res.status_code == 200
    assert body["error"] is None
    assert body["total"] == 5
    assert body["used_query"] == "graph"
    assert body["translated"] is False
    assert body["papers"][0]["id"] == "arxiv:2401.00001"


def test_search_returns_friendly_error_with_200(client, monkeypatch):
    async def boom(data):
        raise FriendlyError("检索请求超时，请检查网络后重试。", False)

    monkeypatch.setattr(search_mod, "search", boom)
    res = client.get("/api/scholar/search", params={"q": "graph"})
    body = res.json()
    assert res.status_code == 200
    assert body["papers"] == []
    assert "超时" in body["error"]


def test_daily_passthrough(client, monkeypatch):
    async def fake_daily(round_index):
        return {"papers": [paper().to_dict()], "date": "2026-11-08", "pool_size": 1, "error": None}

    monkeypatch.setattr(scholar_api, "get_daily", fake_daily)
    body = client.get("/api/scholar/daily", params={"round": 2}).json()
    assert body["date"] == "2026-11-08"
    assert body["papers"][0]["title"] == "Graph Networks"


def test_paper_404(client, monkeypatch):
    async def none(_id):
        return None

    monkeypatch.setattr(search_mod, "get_paper", none)
    assert client.get("/api/scholar/paper", params={"id": "arxiv:1"}).status_code == 404


def test_cite_returns_formatted_text(client):
    res = client.post(
        "/api/scholar/cite",
        json={"paper": paper().to_dict(), "style": "apa"},
    )
    assert res.status_code == 200
    text = res.json()["text"]
    assert text.startswith("Lee, A.")
    assert "Graph Networks." in text


def test_cite_unknown_style_falls_back(client):
    body = client.post(
        "/api/scholar/cite", json={"paper": paper().to_dict(), "style": "chicago"}
    ).json()["text"]
    assert "Graph Networks[EB/OL]" in body


def test_export_bibtex_and_ris(client):
    papers = [paper().to_dict(), paper(id="openalex:W1", source="openalex", title="Other").to_dict()]
    bib = client.post("/api/scholar/export", json={"papers": papers, "format": "bibtex"}).json()
    assert bib["filename"] == "papers.bib"
    assert bib["text"].count("@") == 2

    ris = client.post("/api/scholar/export", json={"papers": papers, "format": "ris"}).json()
    assert ris["filename"] == "papers.ris"
    assert ris["text"].count("TY  - ") == 2


def test_export_empty_is_rejected(client):
    res = client.post("/api/scholar/export", json={"papers": [], "format": "bibtex"})
    assert res.status_code == 400


def test_import_downloads_and_registers(client, library, monkeypatch):
    async def fake_get(_id):
        return paper()

    async def fake_fetch(url):
        assert url == "https://arxiv.org/pdf/2401.00001"
        return b"%PDF-1.4 fake"

    monkeypatch.setattr(search_mod, "get_paper", fake_get)
    monkeypatch.setattr(search_mod, "fetch_pdf", fake_fetch)

    body = client.post("/api/scholar/import", json={"id": "arxiv:2401.00001"}).json()
    assert body["imported"] is True
    assert body["already"] is False
    assert body["paper"]["id"] == "p_001"
    assert len(library.added) == 1
    filename, data, meta = library.added[0]
    assert meta["source_id"] == "arxiv:2401.00001"
    assert meta["source"] == "arxiv"
    assert data.startswith(b"%PDF")


def test_import_is_idempotent_by_source(client, library, monkeypatch):
    library.store.by_source["arxiv:2401.00001"] = {
        "id": "p_existing",
        "title": "Graph Networks",
    }
    async def fake_get(_id):
        return paper()

    monkeypatch.setattr(search_mod, "get_paper", fake_get)
    body = client.post("/api/scholar/import", json={"id": "arxiv:2401.00001"}).json()
    assert body["already"] is True
    assert body["imported"] is False
    assert body["paper"]["id"] == "p_existing"
    assert library.added == []


def test_import_without_pdf_is_400(client, monkeypatch):
    async def fake_get(_id):
        return paper(pdf_url=None, doi=None)

    monkeypatch.setattr(search_mod, "get_paper", fake_get)
    res = client.post("/api/scholar/import", json={"id": "arxiv:2401.00001"})
    assert res.status_code == 400
    assert "没有可下载" in res.json()["detail"]


def test_import_unknown_paper_is_404(client, monkeypatch):
    async def none(_id):
        return None

    monkeypatch.setattr(search_mod, "get_paper", none)
    assert client.post("/api/scholar/import", json={"id": "arxiv:1"}).status_code == 404


def test_fulltext_error_is_reported(client, monkeypatch):
    async def boom(_doi):
        raise FriendlyError("免费全文查询服务暂时不可用（429），请稍后重试。", True)

    async def no_pdf(_doi):
        return None

    monkeypatch.setattr(search_mod, "find_free_full_text", boom)
    monkeypatch.setattr(search_mod, "find_s2_open_pdf", no_pdf)
    monkeypatch.setattr(search_mod, "find_core_pdf", no_pdf)
    body = client.get("/api/scholar/fulltext", params={"doi": "10.1/x"}).json()
    assert body["pdf_url"] is None
    assert "429" in body["error"]


def test_fulltext_falls_back_to_semantic_scholar(client, monkeypatch):
    async def empty(_doi):
        return {
            "pdf_url": None,
            "pdf_urls": [],
            "landing_url": None,
            "oa_status": None,
            "error": None,
        }

    async def s2(_doi):
        return "https://s2/pdf"

    async def no_core(_doi):
        return None

    monkeypatch.setattr(search_mod, "find_free_full_text", empty)
    monkeypatch.setattr(search_mod, "find_s2_open_pdf", s2)
    monkeypatch.setattr(search_mod, "find_core_pdf", no_core)
    body = client.get("/api/scholar/fulltext", params={"doi": "10.1/x"}).json()
    assert body["pdf_url"] == "https://s2/pdf"
    assert body["error"] is None


def test_sources_reports_disabled_core(client, monkeypatch):
    monkeypatch.delenv("PAPEREAD_CORE_API_KEY", raising=False)
    body = client.get("/api/scholar/sources").json()
    assert "core" in body["disabled"]


def test_import_falls_back_to_next_pdf_candidate(client, library, monkeypatch):
    """出版商 PDF 403 时换下一个候选（例如 PMC 镜像）。"""
    async def fake_get(_id):
        return paper(pdf_url="https://publisher/blocked.pdf", doi="10.1/x")

    async def fake_fetch(url):
        if "publisher" in url:
            raise FriendlyError("全文下载失败（403），请稍后重试。", False)
        return b"%PDF-1.4 mirror"

    async def fake_lookup(_paper):
        return ["https://publisher/blocked.pdf", "https://pmc/ok.pdf"]

    monkeypatch.setattr(search_mod, "get_paper", fake_get)
    monkeypatch.setattr(search_mod, "fetch_pdf", fake_fetch)
    monkeypatch.setattr(search_mod, "resolve_pdf_urls", fake_lookup)

    body = client.post("/api/scholar/import", json={"id": "arxiv:2401.00001"}).json()
    assert body["imported"] is True
    assert library.added[0][1] == b"%PDF-1.4 mirror"


def test_import_reports_last_download_error(client, monkeypatch):
    async def fake_get(_id):
        return paper(pdf_url="https://publisher/blocked.pdf")

    async def fake_fetch(_url):
        raise FriendlyError("全文下载失败（403），请稍后重试。", False)

    monkeypatch.setattr(search_mod, "get_paper", fake_get)
    monkeypatch.setattr(search_mod, "fetch_pdf", fake_fetch)
    res = client.post("/api/scholar/import", json={"id": "arxiv:2401.00001"})
    assert res.status_code == 400
    assert "403" in res.json()["detail"]


def test_import_route_does_not_block_on_network(monkeypatch):
    """导入失败（下载报错）时返回错误码，而不是 500。"""
    app = FakeLibrary().app()

    async def fake_get(_id):
        return paper()

    async def fake_fetch(_url):
        raise FriendlyError("全文下载超时，请稍后重试。", True)

    monkeypatch.setattr(search_mod, "get_paper", fake_get)
    monkeypatch.setattr(search_mod, "fetch_pdf", fake_fetch)
    with TestClient(app) as c:
        res = c.post("/api/scholar/import", json={"id": "arxiv:2401.00001"})
    assert res.status_code == 502
    assert "超时" in res.json()["detail"]


def test_asyncio_helpers_are_used_consistently():
    """路由里的异步函数都能在事件循环中跑（防止手写成同步）。"""
    from server.scholar_api import build_router as build

    router = build(FakeStore(), lambda *a: {}, lambda r: r)
    for route in router.routes:
        assert asyncio.iscoroutinefunction(route.endpoint) or route.path == "/api/scholar/meta"
