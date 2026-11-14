"""出处接口（`/api/papers/{id}/block/{id}/provenance`）的单元测试。

接口负责：把一段正文里的引用标记落到参考文献、按块缓存、并为前几条抓摘要。
这里覆盖 404/409 早退、空文本与无引用、全部解析、部分未解析、缓存版本与
签名失效、摘要超时降级、条数上限与截断标记，以及损坏缓存的自我恢复。
"""

from __future__ import annotations

import asyncio
import importlib
import json

import pytest
from fastapi.testclient import TestClient

TOKEN = "prov-token"

PARA = {
    "id": "para1",
    "order": 0,
    "page": 1,
    "kind": "para",
    "text": "As shown in [1], the method works well.",
    "bbox": [60, 100, 540, 120],
}
REF = {
    "id": "ref1",
    "order": 1,
    "page": 1,
    "kind": "reference",
    "text": "[1] Kipf, T. and Welling, M. 2016. Semi-supervised classification with graph networks.",
    "bbox": [60, 200, 540, 220],
}
REF2 = {
    "id": "ref2",
    "order": 2,
    "page": 1,
    "kind": "reference",
    "text": "[2] Smith, J. 2019. Another sufficiently long reference title for parsing.",
    "bbox": [60, 230, 540, 250],
}


def boot(monkeypatch, tmp_path, blocks=None):
    monkeypatch.setenv("PAPEREAD_TOKEN", TOKEN)
    monkeypatch.setenv("PAPEREAD_PORT", "8126")
    monkeypatch.setenv("PAPEREAD_DATA", str(tmp_path / "lib"))
    monkeypatch.setenv("PAPEREAD_MOCK", "1")
    monkeypatch.setenv("PAPEREAD_PANDOC_AUTODOWNLOAD", "0")

    import server.main as main

    importlib.reload(main)
    main.store.papers_dir = tmp_path / "papers"
    main.store.papers_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main.store, "get", lambda pid: {"id": pid} if pid == "p1" else None)
    monkeypatch.setattr(main.store, "read_blocks", lambda pid: blocks if pid == "p1" else None)
    # 断言「不应 500」时拿到真实响应而不是被 TestClient 抛出
    return main, TestClient(main.app, raise_server_exceptions=False)


def headers():
    return {"X-Paperead-Token": TOKEN}


def url(block_id: str = "para1") -> str:
    return f"/api/papers/p1/block/{block_id}/provenance"


def fake_enrich(calls, snippet: str = "the snippet"):
    async def _enrich(ref, text):
        calls.append((ref.index, text))
        return {
            "marker": "",
            "ref": ref.to_dict(),
            "snippet": snippet,
            "url": "https://example.org/paper",
            "source": "openalex:doi",
        }

    return _enrich


# ── 早退分支 ──────────────────────────────────────────


def test_provenance_rejects_unknown_paper_and_block(monkeypatch, tmp_path):
    main, client = boot(monkeypatch, tmp_path, blocks=[dict(PARA), dict(REF)])
    monkeypatch.setattr(main, "enrich_provenance", fake_enrich([]))
    with client:
        assert (
            client.get("/api/papers/nope/block/para1/provenance", headers=headers()).status_code
            == 404
        )
        assert client.get(url("missing"), headers=headers()).status_code == 404


def test_provenance_reports_pending_parse(monkeypatch, tmp_path):
    _main, client = boot(monkeypatch, tmp_path, blocks=None)
    with client:
        assert client.get(url(), headers=headers()).status_code == 409


def test_provenance_returns_empty_for_blank_and_citation_free_blocks(monkeypatch, tmp_path):
    blank = {**PARA, "id": "blank", "text": ""}
    plain = {**PARA, "id": "plain", "text": "No citations in this paragraph."}
    main, client = boot(monkeypatch, tmp_path, blocks=[blank, plain, dict(REF)])
    monkeypatch.setattr(main, "enrich_provenance", fake_enrich([]))

    with client:
        for block_id in ("blank", "plain"):
            body = client.get(url(block_id), headers=headers()).json()
            assert body == {
                "block_id": block_id,
                "citations": [],
                "unresolved": [],
                "truncated": False,
            }
    # 空结果不写缓存，避免无意义文件
    assert not (tmp_path / "papers" / "p1.prov2.json").exists()


# ── 正常解析与缓存 ────────────────────────────────────


def test_provenance_resolves_enriches_and_caches(monkeypatch, tmp_path):
    blocks = [dict(PARA), dict(REF)]
    main, client = boot(monkeypatch, tmp_path, blocks=blocks)
    calls: list[tuple[int, str]] = []
    monkeypatch.setattr(main, "enrich_provenance", fake_enrich(calls))

    with client:
        first = client.get(url(), headers=headers())
        assert first.status_code == 200
        body = first.json()
        assert body["block_id"] == "para1"
        assert [c["marker"] for c in body["citations"]] == ["[1]"]
        assert body["citations"][0]["snippet"] == "the snippet"
        assert body["citations"][0]["ref"]["index"] == 1
        assert body["unresolved"] == []
        assert body["truncated"] is False
        assert calls == [(1, PARA["text"])]

        cache_path = tmp_path / "papers" / "p1.prov2.json"
        saved = json.loads(cache_path.read_text(encoding="utf-8"))
        assert saved["version"] == main.PROV_CACHE_VERSION
        entry = saved["blocks"]["para1"]
        assert entry["sig"] == main._prov_sig(PARA)
        assert [c["marker"] for c in entry["citations"]] == ["[1]"]
        assert entry["truncated"] is False

        # 同一块第二次命中缓存，不再调用摘要接口
        second = client.get(url(), headers=headers())
        assert second.json() == body
        assert len(calls) == 1

        # 块文本变化 → 签名失效 → 重新解析
        blocks[0]["text"] = "Updated wording still cites [1]."
        third = client.get(url(), headers=headers())
        assert third.status_code == 200
        assert len(calls) == 2
        assert calls[1][1] == blocks[0]["text"]
        saved = json.loads(cache_path.read_text(encoding="utf-8"))
        assert saved["blocks"]["para1"]["sig"] == main._prov_sig(blocks[0])


def test_provenance_ignores_stale_and_corrupt_cache(monkeypatch, tmp_path):
    blocks = [dict(PARA), dict(REF)]
    main, client = boot(monkeypatch, tmp_path, blocks=blocks)
    calls: list[tuple[int, str]] = []
    monkeypatch.setattr(main, "enrich_provenance", fake_enrich(calls))
    cache_path = tmp_path / "papers" / "p1.prov2.json"

    cache_path.write_text(json.dumps({"version": 1, "blocks": {}}), encoding="utf-8")
    with client:
        assert client.get(url(), headers=headers()).status_code == 200
        assert len(calls) == 1
        saved = json.loads(cache_path.read_text(encoding="utf-8"))
        assert saved["version"] == main.PROV_CACHE_VERSION

        # 缓存文件损坏：按空缓存处理并重建
        cache_path.write_text("{not-json", encoding="utf-8")
        blocks[0]["text"] = "Another wording citing [1]."
        assert client.get(url(), headers=headers()).status_code == 200
        assert len(calls) == 2
        saved = json.loads(cache_path.read_text(encoding="utf-8"))
        assert saved["blocks"]["para1"]["sig"] == main._prov_sig(blocks[0])


def test_provenance_recovers_from_malformed_blocks_cache(monkeypatch, tmp_path):
    # blocks 被写成列表（旧结构/手工修改）时不能 500
    main, client = boot(monkeypatch, tmp_path, blocks=[dict(PARA), dict(REF)])
    monkeypatch.setattr(main, "enrich_provenance", fake_enrich([]))
    cache_path = tmp_path / "papers" / "p1.prov2.json"
    cache_path.write_text(
        json.dumps({"version": main.PROV_CACHE_VERSION, "blocks": []}), encoding="utf-8"
    )

    with client:
        res = client.get(url(), headers=headers())
    assert res.status_code == 200
    assert [c["marker"] for c in res.json()["citations"]] == ["[1]"]
    saved = json.loads(cache_path.read_text(encoding="utf-8"))
    assert isinstance(saved["blocks"], dict)
    assert saved["blocks"]["para1"]["sig"] == main._prov_sig(PARA)


# ── 未解析与部分解析 ──────────────────────────────────


def test_provenance_reports_unresolved_markers(monkeypatch, tmp_path):
    para = {**PARA, "text": "See [9] and [42] for details."}
    main, client = boot(monkeypatch, tmp_path, blocks=[para, dict(REF)])
    calls: list[tuple[int, str]] = []
    monkeypatch.setattr(main, "enrich_provenance", fake_enrich(calls))

    with client:
        body = client.get(url(), headers=headers()).json()
    assert body["citations"] == []
    assert body["unresolved"] == ["[9]", "[42]"]
    assert body["truncated"] is False
    assert calls == []


def test_provenance_mixes_resolved_and_unresolved(monkeypatch, tmp_path):
    para = {**PARA, "text": "See [1] and [9]."}
    main, client = boot(monkeypatch, tmp_path, blocks=[para, dict(REF)])
    monkeypatch.setattr(main, "enrich_provenance", fake_enrich([]))

    with client:
        body = client.get(url(), headers=headers()).json()
    assert [c["marker"] for c in body["citations"]] == ["[1]"]
    assert body["unresolved"] == ["[9]"]


# ── 摘要预算与上限 ────────────────────────────────────


def test_provenance_falls_back_when_enrich_times_out(monkeypatch, tmp_path):
    main, client = boot(monkeypatch, tmp_path, blocks=[dict(PARA), dict(REF)])

    async def slow(ref, text):
        await asyncio.sleep(0.2)
        return {
            "marker": "",
            "ref": ref.to_dict(),
            "snippet": "late",
            "url": "u",
            "source": "s",
        }

    monkeypatch.setattr(main, "enrich_provenance", slow)
    monkeypatch.setattr(main, "PROV_TIMEOUT", 0.01)

    with client:
        body = client.get(url(), headers=headers()).json()
    citation = body["citations"][0]
    assert citation["marker"] == "[1]"
    assert citation["snippet"] == ""
    # 无 DOI/arXiv 时链接兜底为 none
    assert citation["source"] == "none"
    assert citation["url"] == ""


def test_provenance_uses_ref_only_beyond_snippet_top(monkeypatch, tmp_path):
    para = {**PARA, "text": "Both [1] and [2] matter."}
    main, client = boot(monkeypatch, tmp_path, blocks=[para, dict(REF), dict(REF2)])
    calls: list[tuple[int, str]] = []
    monkeypatch.setattr(main, "enrich_provenance", fake_enrich(calls))
    monkeypatch.setattr(main, "PROV_SNIPPET_TOP", 1)

    with client:
        body = client.get(url(), headers=headers()).json()
    assert [c["marker"] for c in body["citations"]] == ["[1]", "[2]"]
    assert [c["ref"]["index"] for c in body["citations"]] == [1, 2]
    assert len(calls) == 1
    assert calls[0][0] == 1
    assert body["citations"][1]["snippet"] == ""


def test_provenance_truncates_long_resolution(monkeypatch, tmp_path):
    para = {**PARA, "text": "Both [1] and [2] matter."}
    main, client = boot(monkeypatch, tmp_path, blocks=[para, dict(REF), dict(REF2)])
    calls: list[tuple[int, str]] = []
    monkeypatch.setattr(main, "enrich_provenance", fake_enrich(calls))
    monkeypatch.setattr(main, "PROV_MAX", 1)

    with client:
        body = client.get(url(), headers=headers()).json()
    assert body["truncated"] is True
    assert [c["marker"] for c in body["citations"]] == ["[1]"]
    assert len(calls) == 1


def test_provenance_keeps_truncated_flag_when_all_entries_are_dropped(monkeypatch, tmp_path):
    para = {**PARA, "text": "Both [1] and [2] matter."}
    main, client = boot(monkeypatch, tmp_path, blocks=[para, dict(REF), dict(REF2)])
    monkeypatch.setattr(main, "enrich_provenance", fake_enrich([]))
    monkeypatch.setattr(main, "PROV_MAX", 0)

    with client:
        body = client.get(url(), headers=headers()).json()
    assert body["citations"] == []
    assert body["unresolved"] == []
    # 全部条目都因上限被丢弃时，仍应告知前端发生过截断
    assert body["truncated"] is True