"""Phase 0 翻译契约：可译清单、状态、验收报告与 0 覆盖守卫。"""

from __future__ import annotations

import asyncio
import importlib
import json
import time

import pymupdf
import pytest

from paperead.translation_manifest import (
    build_manifest,
    build_report,
    classify_error,
    coverage_guard,
    read_report,
)

CONFIG = {
    "base_url": "https://api.example.com/v1",
    "api_key": "test-key-123456",
    "model": "test-model",
}


def _block(
    bid: str,
    text: str,
    kind: str = "para",
    *,
    translatable: bool = True,
    page: int = 1,
    translated: str = "",
) -> dict:
    return {
        "id": bid,
        "order": 0,
        "page": page,
        "kind": kind,
        "text": text,
        "bbox": [0, 0, 100, 20],
        "translatable": translatable,
        "translated": translated,
    }


def _sample_blocks() -> list[dict]:
    return [
        _block("b1", "This paper studies protein ligand binding affinity prediction."),
        _block("b2", "Introduction", kind="heading"),
        _block("b3", "Figure 1. Binding affinity results.", kind="caption", translatable=False),
        _block(
            "b4",
            "We evaluate the model on multiple benchmark datasets.",
            translated="我们在多个基准数据集上评估模型。",
        ),
        _block("b5", "Smith et al. 2020.", kind="reference", translatable=False),
        _block("b6", "100%"),
        _block("b7", ""),
        _block("b8", "figure label", kind="figure", translatable=False),
        _block("b9", "E = mc2", kind="equation", translatable=False),
    ]


def test_manifest_is_single_source_for_eligibility() -> None:
    manifest = build_manifest(_sample_blocks(), "p_test", "zh-Hans")

    assert manifest["eligible_total"] == 4
    assert manifest["translated"] == 1
    assert manifest["pending"] == 3
    assert manifest["skipped"] == 5
    assert manifest["coverage"] == 0.25
    assert set(manifest["eligible_ids"]) == {"b1", "b2", "b3", "b4"}
    assert manifest["excluded"]["reference"] == 1
    assert manifest["excluded"]["fragment"] == 1
    assert manifest["excluded"]["empty"] == 1
    assert manifest["excluded"]["figure"] == 1
    assert manifest["excluded"]["equation"] == 1

    statuses = {row["id"]: row for row in manifest["blocks"]}
    assert statuses["b4"]["status"] == "done"
    assert statuses["b5"]["status"] == "skipped"
    assert statuses["b5"]["reason"] == "reference"


def test_manifest_keeps_previous_failures_but_never_overrides_translation() -> None:
    blocks = _sample_blocks()
    report = {
        "failures": [
            {"block_id": "b1", "code": "network", "message": "连接失败"},
            {"block_id": "b4", "code": "rate_limit", "message": "限流"},
        ]
    }
    manifest = build_manifest(blocks, "p_test", "zh-Hans", report=report)
    statuses = {row["id"]: row for row in manifest["blocks"]}

    assert statuses["b1"]["status"] == "failed"
    assert statuses["b1"]["code"] == "network"
    # b4 已有译文，旧失败不能把它重新标成 failed。
    assert statuses["b4"]["status"] == "done"
    assert manifest["failed"] == 1
    assert manifest["pending"] == 2


def test_report_status_and_page_coverage() -> None:
    blocks = _sample_blocks()
    manifest = build_manifest(blocks, "p_test", "zh-Hans")
    report = build_report(
        manifest,
        started_at=time.time() - 1,
        failures=[{"block_id": "b2", "code": "timeout", "message": "超时"}],
        new_translated=0,
        usage={"calls": 3},
    )

    assert report["status"] == "partial"
    assert report["failed"] == 1
    assert report["coverage"] == 0.25
    assert report["pages"][0]["eligible"] == 4
    assert report["pages"][0]["translated"] == 1
    assert report["failures"][0]["code"] == "timeout"
    assert report["elapsed_ms"] >= 0


def test_report_marks_blocked_and_succeeded() -> None:
    blocked = build_manifest(
        [_block("b1", "Smith et al.", kind="reference", translatable=False)],
        "p_test",
    )
    assert build_report(blocked)["status"] == "blocked"

    done_blocks = [_block("b1", "A full English paragraph.", translated="一段完整的中文译文。")]
    succeeded = build_manifest(done_blocks, "p_test")
    assert build_report(succeeded)["status"] == "succeeded"


def test_coverage_guard_blocks_empty_outputs() -> None:
    no_eligible = build_manifest(
        [_block("b1", "Smith et al.", kind="reference", translatable=False)]
    )
    allowed, code, message = coverage_guard(no_eligible)
    assert allowed is False
    assert code == "no_eligible"
    assert "扫描版" in message

    zero = build_manifest(_sample_blocks()[:1])
    allowed, code, message = coverage_guard(zero)
    assert allowed is False
    assert code == "zero_coverage"
    assert "空译文" in message

    allowed, code, _ = coverage_guard(
        build_manifest([_block("b1", "English.", translated="中文。")])
    )
    assert allowed is True
    assert code == ""


def test_classify_error_maps_model_failures() -> None:
    class AuthenticationError(Exception):
        pass

    class RateLimitError(Exception):
        pass

    class APIConnectionError(Exception):
        pass

    assert classify_error(AuthenticationError("401"))[0] == "auth"
    assert classify_error(RateLimitError("429 rate limit"))[0] == "rate_limit"
    assert classify_error(APIConnectionError("Connection error."))[0] == "network"
    assert classify_error(TimeoutError("timed out"))[0] == "timeout"
    assert classify_error(RuntimeError("模型返回空译文"))[0] == "empty_content"
    assert classify_error(RuntimeError("something odd"))[0] == "unknown"


def _load_main(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPEREAD_DATA", str(tmp_path / "lib"))
    monkeypatch.setenv("PAPEREAD_MOCK", "1")
    monkeypatch.delenv("PAPEREAD_TOKEN", raising=False)

    import server.main as main

    importlib.reload(main)
    return main


def _add_paper(main, name: str = "contract.pdf") -> str:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Translation contract", fontsize=16)
    page.insert_text((72, 110), "Body text.", fontsize=10)
    data = doc.tobytes()
    doc.close()

    row = main.store.add(name, data)
    main.store.update(row["id"], status="ready", pages=1)
    return row["id"]


def _write_blocks(main, paper_id: str, blocks: list[dict]) -> None:
    main.store.write_blocks(paper_id, blocks)


def _sse_events(text: str) -> list[dict]:
    events: list[dict] = []
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload:
            events.append(json.loads(payload))
    return events


def test_manifest_route_and_zero_coverage_build_guard(monkeypatch, tmp_path) -> None:
    main = _load_main(monkeypatch, tmp_path)
    paper_id = _add_paper(main)
    _write_blocks(
        main,
        paper_id,
        [
            _block("b1", "This paragraph has translatable academic content."),
            _block("b2", "Smith et al. 2020.", kind="reference", translatable=False),
        ],
    )
    from fastapi.testclient import TestClient

    client = TestClient(main.app)
    manifest = client.get(f"/api/papers/{paper_id}/translation/manifest").json()
    assert manifest["eligible_total"] == 1
    assert manifest["pending"] == 1
    assert manifest["coverage"] == 0.0

    res = client.post(f"/api/papers/{paper_id}/translated-pdf/build")
    assert res.status_code == 409
    assert "空译文" in res.json()["detail"]
    assert res.headers.get("x-translation-code") == "zero_coverage"


def test_stream_emits_report_with_failures(monkeypatch, tmp_path) -> None:
    main = _load_main(monkeypatch, tmp_path)
    paper_id = _add_paper(main, "stream.pdf")
    _write_blocks(
        main,
        paper_id,
        [
            _block("b1", "This paragraph should translate successfully.", page=1),
            _block("b2", "This paragraph will fail with a connection error.", page=1),
            _block("b3", "Smith et al. 2020.", kind="reference", translatable=False),
        ],
    )

    async def fake_glossary(*args, **kwargs):
        return {}

    async def fake_translate(block, glossary, prev_tail, next_head, **kwargs):
        if "fail" in block.text:
            raise ConnectionError("Connection error.")
        return "这是顺利翻译完成的整段中文译文。"

    monkeypatch.setattr(main, "build_glossary", fake_glossary)
    monkeypatch.setattr(main, "translate_block", fake_translate)

    from fastapi.testclient import TestClient

    client = TestClient(main.app)
    res = client.post(
        f"/api/papers/{paper_id}/translate/stream",
        json={
            "config": CONFIG,
            "pages": None,
            "target_lang": "zh-Hans",
            "auto_highlights": False,
        },
    )
    assert res.status_code == 200, res.text
    events = _sse_events(res.text)
    done = [e for e in events if e.get("type") == "done"]
    assert done, events
    summary = done[0]
    assert summary["total"] == 2
    assert summary["translated"] == 1
    assert summary["failed"] == 1
    assert summary["skipped"] == 1
    assert summary["coverage"] == 0.5
    assert summary["status"] == "partial"

    report = read_report(tmp_path / "lib", paper_id, "zh-Hans")
    assert report is not None
    assert report["failed"] == 1
    assert report["failures"][0]["block_id"] == "b2"
    assert report["failures"][0]["code"] == "network"

    manifest = client.get(f"/api/papers/{paper_id}/translation/manifest").json()
    statuses = {row["id"]: row for row in manifest["blocks"]}
    assert statuses["b1"]["status"] == "done"
    assert statuses["b2"]["status"] == "failed"
    assert statuses["b3"]["status"] == "skipped"


def _add_blank_paper(main, name: str = "blank.pdf") -> str:
    doc = pymupdf.open()
    doc.new_page()
    data = doc.tobytes()
    doc.close()
    row = main.store.add(name, data)
    return row["id"]


def test_empty_parse_is_failed_not_ready(monkeypatch, tmp_path) -> None:
    """扫描版/无文本层：标记 failed + 原因，不能再显示 ready 并静默 0 待译。"""
    main = _load_main(monkeypatch, tmp_path)
    paper_id = _add_blank_paper(main)

    asyncio.run(main._parse_task(paper_id))

    row = main.store.get(paper_id)
    assert row["status"] == "failed"
    assert "扫描版" in row["error"]
    assert main.store.read_blocks(paper_id) == []

    from fastapi.testclient import TestClient

    client = TestClient(main.app)
    manifest = client.get(f"/api/papers/{paper_id}/translation/manifest").json()
    assert manifest["eligible_total"] == 0
    assert manifest["blocks_total"] == 0


def test_only_engine_pdf_is_served(monkeypatch, tmp_path) -> None:
    """译文 PDF 只认引擎登记：内部回写器留下的旧文件不得被当成译文。"""
    main = _load_main(monkeypatch, tmp_path)
    paper_id = _add_paper(main, "stale.pdf")
    _write_blocks(
        main,
        paper_id,
        [_block("b1", "This paragraph has not been translated yet.")],
    )

    out = main._engine_output_dir(paper_id, "zh-Hans")
    out.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Looks like a translated PDF", fontsize=12)
    (out / "translated.pdf").write_bytes(doc.tobytes())
    doc.close()
    (out / "meta.json").write_text(
        json.dumps({"algo_ver": "p2-autofit-v78", "lang": "zh-Hans"}),
        encoding="utf-8",
    )
    from fastapi.testclient import TestClient

    client = TestClient(main.app)
    head = client.head(f"/api/papers/{paper_id}/translated-pdf")
    assert head.status_code == 404
    assert head.headers.get("x-translation-code") == "engine_missing"
    assert client.get(f"/api/papers/{paper_id}/translated-pdf").status_code == 404

    # 登记引擎成品后，仍要先过「0 覆盖」守卫：不能把没翻译的原文当译文交付
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Engine output", fontsize=12)
    (out / "babeldoc.mono.pdf").write_bytes(doc.tobytes())
    doc.close()
    (out / "engine.json").write_text(
        json.dumps(
            {
                "provider": "babeldoc",
                "file": "babeldoc.mono.pdf",
                "lang": "zh-Hans",
                "pages": 1,
            }
        ),
        encoding="utf-8",
    )
    head = client.head(f"/api/papers/{paper_id}/translated-pdf")
    assert head.status_code == 404
    assert head.headers.get("x-translation-code") == "zero_coverage"

    # 有译文 + 引擎成品：按引擎文件交付
    blocks = main.store.read_blocks(paper_id)
    blocks[0]["translated"] = "这一段已经翻译完成。"
    main.store.write_blocks(paper_id, blocks)
    head = client.head(f"/api/papers/{paper_id}/translated-pdf")
    assert head.status_code == 204
    served = client.get(f"/api/papers/{paper_id}/translated-pdf")
    assert served.status_code == 200
    assert served.content.startswith(b"%PDF")


def test_build_reports_engine_state(monkeypatch, tmp_path) -> None:
    main = _load_main(monkeypatch, tmp_path)
    paper_id = _add_paper(main, "partial.pdf")
    _write_blocks(
        main,
        paper_id,
        [
            _block(
                "b1",
                "This paragraph already has a translation.",
                translated="这一段已经有中文译文。",
            ),
            _block("b2", "This paragraph is still pending translation."),
        ],
    )
    from fastapi.testclient import TestClient

    client = TestClient(main.app)
    res = client.post(f"/api/papers/{paper_id}/translated-pdf/build")
    assert res.status_code == 409
    assert res.headers.get("x-translation-code") == "engine_missing"
    assert "全文翻译" in res.json()["detail"]

    out = main._engine_output_dir(paper_id, "zh-Hans")
    out.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Engine output", fontsize=12)
    (out / "babeldoc.mono.pdf").write_bytes(doc.tobytes())
    doc.close()
    (out / "engine.json").write_text(
        json.dumps(
            {
                "provider": "babeldoc",
                "file": "babeldoc.mono.pdf",
                "lang": "zh-Hans",
                "pages": 1,
            }
        ),
        encoding="utf-8",
    )
    res = client.post(f"/api/papers/{paper_id}/translated-pdf/build")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["engine"] == "babeldoc"
    assert body["status"] == "partial"
    assert body["coverage"] == 0.5
    assert body["translation"]["translated"] == 1