"""阅读页问答的联网检索测试：解析、回退、缓存与提示词（不联网）。"""

from __future__ import annotations

import asyncio

import pytest

from paperead.tutor import qa as qa_engine
from paperead.tutor import web

SO360_HTML = """
<li class="res-list">
  <style>.g-v-feedback-tip{color:#333}</style>
  <h3 class="res-title"><a href="https://www.so.com/link?m=abc"
      data-mdurl="https://developer.example.com/a" target="_blank">自注意力
      <em>机制</em>原理</a></h3>
  <div class="res-comm-con"><p></p>
    <span class="res-list-summary" data-result-summary>核心是<em>自注意力</em>,让模型关注所有词</span>
  </div>
  <p class="g-linkinfo"><cite><a href="https://www.so.com/link?m=abc">developer.example.com</a></cite></p>
</li>
<li class="res-list">
  <h3 class="res-title"><a href="https://ai.so.com/search/xyz">站内 AI 卡片</a></h3>
</li>
<li class="res-list">
  <h3 class="res-title"><a href="https://www.so.com/link?m=def" data-mdurl="https://blog.example.com/b">第二篇</a></h3>
  <p class="res-desc"><span data-result-summary>对比学习的三个步骤</span></p>
</li>
"""

BING_HTML = """
<ol id="b_results">
  <li class="b_algo"><h2><a href="https://www.bing.com/ck/a?!&amp;&amp;p=1&amp;u=a1aHR0cHM6Ly9leGFtcGxlLmNvbS9wYXBlcg">
    Transformer 模型详解</a></h2><p>Transformer 的整体结构。</p></li>
</ol>
"""

OPENALEX_PAYLOAD = {
    "results": [
        {
            "display_name": "Attention Is All You Need",
            "publication_year": 2017,
            "doi": "https://doi.org/10.1000/xyz",
            "primary_location": {"landing_page_url": None},
            "abstract_inverted_index": {"We": [0], "propose": [1], "Transformer": [2]},
        },
        {"display_name": "", "id": "https://openalex.org/W1"},
    ]
}


@pytest.fixture(autouse=True)
def clear_cache(monkeypatch):
    web._cache.clear()
    monkeypatch.setattr(web, "_academic_block_until", 0.0)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("PAPEREAD_WEB_SEARCH", raising=False)
    yield
    web._cache.clear()


def test_parse_360_uses_real_url_and_summary():
    hits = web.parse_360(SO360_HTML, 4)
    assert [h["title"] for h in hits] == ["自注意力 机制 原理", "第二篇"]
    assert hits[0]["url"] == "https://developer.example.com/a"
    assert "自注意力" in hits[0]["content"]
    assert hits[1]["content"] == "对比学习的三个步骤"
    # 站内 AI 卡片没有真实站点，必须被过滤
    assert all("so.com" not in h["url"] for h in hits)
    assert {h["engine"] for h in hits} == {"so360"}


def test_parse_bing_decodes_redirect_url():
    hits = web.parse_bing(BING_HTML, 3)
    assert len(hits) == 1
    assert hits[0]["url"] == "https://example.com/paper"
    assert hits[0]["engine"] == "bing"


def test_parse_openalex_rebuilds_abstract_and_skips_untitled():
    hits = web.parse_openalex(OPENALEX_PAYLOAD, 3)
    assert len(hits) == 1
    assert hits[0]["title"].startswith("（2017）Attention Is All You Need")
    assert hits[0]["url"] == "https://doi.org/10.1000/xyz"
    assert hits[0]["content"] == "We propose Transformer"


def test_search_falls_back_to_360_without_any_key(monkeypatch):
    calls: list[str] = []

    async def no_tavily(query, limit):
        calls.append("tavily")
        return []

    async def so360(query, limit):
        calls.append("so360")
        return [{"title": "t", "url": "https://e.com", "content": "c", "engine": "so360"}]

    async def bing(query, limit):
        calls.append("bing")
        return []

    monkeypatch.setattr(web, "_tavily", no_tavily)
    monkeypatch.setattr(web, "_so360", so360)
    monkeypatch.setattr(web, "_bing", bing)

    hits = asyncio.run(web.search("注意力机制 原理", 3))
    assert calls == ["tavily", "so360"]
    assert hits[0]["engine"] == "so360"


def test_search_falls_back_to_bing_when_360_empty(monkeypatch):
    async def empty(query, limit):
        return []

    async def bing(query, limit):
        return [{"title": "b", "url": "https://e.com/b", "content": "", "engine": "bing"}]

    monkeypatch.setattr(web, "_tavily", empty)
    monkeypatch.setattr(web, "_so360", empty)
    monkeypatch.setattr(web, "_bing", bing)
    monkeypatch.setattr(web, "_ddg", empty)

    hits = asyncio.run(web.search("transformer", 3))
    assert [h["engine"] for h in hits] == ["bing"]


def test_search_is_cached(monkeypatch):
    calls = {"n": 0}

    async def so360(query, limit):
        calls["n"] += 1
        return [{"title": "t", "url": "https://e.com", "content": "c", "engine": "so360"}]

    monkeypatch.setattr(web, "_tavily", lambda q, l: asyncio.sleep(0, result=[]))
    monkeypatch.setattr(web, "_so360", so360)

    asyncio.run(web.search("同一个问题", 3))
    asyncio.run(web.search("同一个问题", 3))
    assert calls["n"] == 1


def test_search_many_interleaves_academic(monkeypatch):
    async def general(query, limit):
        return [
            {"title": f"g{i}", "url": f"https://g.com/{i}", "content": "", "engine": "so360"}
            for i in range(limit)
        ]

    async def academic(query, limit):
        return [{"title": "a0", "url": "https://a.com/0", "content": "", "engine": "openalex"}]

    monkeypatch.setattr(web, "search", general)
    monkeypatch.setattr(web, "search_academic", academic)

    hits = asyncio.run(
        web.search_many(
            ["q1", "q2"], limit=4, total_limit=3, academic=True, academic_query="en words"
        )
    )
    assert len(hits) == 3
    assert "openalex" in {h["engine"] for h in hits}


def test_search_disabled_by_env(monkeypatch):
    monkeypatch.setenv("PAPEREAD_WEB_SEARCH", "off")
    assert asyncio.run(web.search("注意力机制", 3)) == []


def test_filter_relevant_drops_dictionary_results():
    """Bing 把英文术语拆成单词后返回的词典结果必须被剔除。"""
    hits = [
        {"title": "self（英语单词）_百度百科", "url": "https://e.com/1", "content": "英语单词"},
        {"title": "Transformer 模型详解", "url": "https://e.com/2", "content": "注意力机制"},
    ]
    kept = web.filter_relevant("transformer attention mechanism", hits)
    assert [h["url"] for h in kept] == ["https://e.com/2"]


def test_chinese_query_does_not_fall_back_to_bing(monkeypatch):
    """中文查询在 360 失败后不应回退 Bing（实测只会给单字词典结果）。"""
    calls: list[str] = []

    async def empty(query, limit):
        return []

    async def so360(query, limit):
        calls.append("so360")
        return []

    async def bing(query, limit):
        calls.append("bing")
        return [{"title": "自_百度百科", "url": "https://e.com/z", "content": "", "engine": "bing"}]

    monkeypatch.setattr(web, "_tavily", empty)
    monkeypatch.setattr(web, "_so360", so360)
    monkeypatch.setattr(web, "_bing", bing)
    monkeypatch.setattr(web, "_ddg", empty)

    assert asyncio.run(web.search("自注意力机制原理", 3)) == []
    assert calls == ["so360"]


def test_empty_result_is_cached_briefly(monkeypatch):
    monkeypatch.setattr(web, "_EMPTY_CACHE_TTL", -1.0)
    calls = {"n": 0}

    async def empty(query, limit):
        calls["n"] += 1
        return []

    monkeypatch.setattr(web, "_tavily", empty)
    monkeypatch.setattr(web, "_so360", empty)
    monkeypatch.setattr(web, "_ddg", empty)

    asyncio.run(web.search("搜不到的东西 xyz", 3))
    asyncio.run(web.search("搜不到的东西 xyz", 3))
    # 每次搜索依次尝试 tavily / 360 / ddg 三路；空结果 TTL 为 0 时两次都不命中缓存
    assert calls["n"] == 6


def test_needs_external_covers_concept_questions():
    # 概念题即使论文有部分匹配，也应该再往外部找一轮
    assert qa_engine.needs_external("自注意力机制是什么", 2.0) is True
    assert qa_engine.needs_external("2025 年有没有更新的方法", 9.0) is True
    # 论文里明确能答的细节题不强制外搜
    assert qa_engine.needs_external("本文在 ImageNet 上的准确率是多少", 6.0) is False


def test_answer_prompt_allows_web_and_common_sense():
    assert "论文确实没写" in qa_engine.ANSWER_SYS
    assert "（通识）" in qa_engine.ANSWER_SYS
    messages = qa_engine._answer_messages(
        "问题", "【材料】", web_empty=True
    )
    user = messages[-1]["content"]
    assert "【联网状态】" in user
    assert "（通识）" in user


def test_build_context_includes_web_url():
    context = qa_engine.build_context(
        [], None, [{"title": "标题", "url": "https://e.com/x", "content": "正文"}]
    )
    assert "[W1] 标题（https://e.com/x）" in context