"""web_fetch 正文抓取测试：公网校验、HTML 抽取、失败回退（不联网）。"""

from __future__ import annotations

import asyncio

import pytest

from paperead.tutor import web


class FakeResponse:
    def __init__(self, status_code: int = 200, text: str = "", content_type: str = "text/html; charset=utf-8"):
        self.status_code = status_code
        self.text = text
        self.headers = {"content-type": content_type}


class FakeClient:
    response = FakeResponse()

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, **kwargs):
        return FakeClient.response


@pytest.fixture(autouse=True)
def fake_http(monkeypatch):
    monkeypatch.setattr(web.httpx, "AsyncClient", FakeClient)
    yield


def test_public_url_guard():
    assert web._is_public_http_url("https://example.com/a")
    assert web._is_public_http_url("http://arxiv.org/abs/1706.03762")
    for bad in (
        "file:///etc/passwd",
        "http://127.0.0.1:8010/api",
        "http://localhost/x",
        "http://10.0.0.5/x",
        "http://192.168.1.2/x",
        "http://[::1]/x",
        "",
    ):
        assert not web._is_public_http_url(bad), bad


def test_fetch_extracts_html_and_marks_truncation():
    FakeClient.response = FakeResponse(
        text="""
        <html><head><title>示例文章</title><style>.a{color:red}</style></head>
        <body><nav>导航</nav>
          <article><h1>自注意力机制</h1>
          <p>每个位置关注所有位置。</p>
          <script>var x = 1;</script>
          <p>用 Query 与 Key 的相似度加权 Value。</p></article>
          <footer>页脚</footer>
        </body></html>
        """
    )
    got = asyncio.run(web.fetch("https://example.com/a", 40))
    assert got["error"] == ""
    assert got["statusCode"] == 200
    assert got["title"] == "示例文章"
    assert "自注意力机制" in got["content"]
    assert "导航" not in got["content"]
    assert "页脚" not in got["content"]
    assert "var x" not in got["content"]
    assert got["truncated"] is True


def test_fetch_returns_error_on_http_failure():
    FakeClient.response = FakeResponse(status_code=403, text="blocked")
    got = asyncio.run(web.fetch("https://example.com/b"))
    assert got["error"] == "HTTP 403"
    assert got["content"] == ""


def test_fetch_returns_error_when_no_body():
    FakeClient.response = FakeResponse(text="<html><body><script>x()</script></body></html>")
    got = asyncio.run(web.fetch("https://example.com/c"))
    assert got["error"] == "页面没有可提取的正文"


def test_fetch_plain_text_response():
    FakeClient.response = FakeResponse(
        text="纯文本正文，没有 HTML。", content_type="text/plain"
    )
    got = asyncio.run(web.fetch("https://example.com/d", 100))
    assert got["error"] == ""
    assert got["content"] == "纯文本正文，没有 HTML。"