"""Phase 1：服务端持久化翻译任务的启动、进度、失败与续跑。"""

from __future__ import annotations

import asyncio
import importlib
import json
import time

import pymupdf

CONFIG = {
    "base_url": "https://api.example.com/v1",
    "api_key": "test-key-123456",
    "model": "test-model",
}

TERMINAL = {"succeeded", "partial", "failed", "cancelled"}


class AuthenticationError(Exception):
    """仅用类名触发 classify_error 的 auth 分支。"""


def _load_main(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPEREAD_DATA", str(tmp_path / "lib"))
    monkeypatch.setenv("PAPEREAD_MOCK", "1")
    monkeypatch.delenv("PAPEREAD_TOKEN", raising=False)
    import server.main as main

    importlib.reload(main)
    return main


def _add_paper(main, name: str = "job.pdf") -> str:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Translation job", fontsize=16)
    page.insert_text((72, 110), "Body text.", fontsize=10)
    data = doc.tobytes()
    doc.close()
    row = main.store.add(name, data)
    main.store.update(row["id"], status="ready", pages=1)
    return row["id"]


def _block(bid: str, text: str, *, page: int = 1, kind: str = "para", translatable: bool = True) -> dict:
    return {
        "id": bid,
        "order": 0,
        "page": page,
        "kind": kind,
        "text": text,
        "bbox": [0, 0, 100, 20],
        "translatable": translatable,
        "translated": "",
    }


def _write_blocks(main, paper_id: str) -> None:
    main.store.write_blocks(
        paper_id,
        [
            _block("b1", "This paragraph should translate successfully."),
            _block("b2", "This paragraph also needs translation."),
            _block("b3", "Smith et al. 2020.", kind="reference", translatable=False),
        ],
    )


def _fake_translate(text: str):
    async def fake(block, glossary, prev_tail, next_head, **kwargs):
        return f"中文译文：{block.text[:20]}"

    return fake


def _wait_job(client, paper_id: str, job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        res = client.get(f"/api/papers/{paper_id}/translation/jobs/{job_id}")
        assert res.status_code == 200, res.text
        last = res.json()
        if last.get("status") in TERMINAL:
            return last
        time.sleep(0.05)
    raise AssertionError(f"任务超时：{last}")


def test_job_runs_in_background_and_publishes_blocks(monkeypatch, tmp_path) -> None:
    """整篇任务只走引擎：引擎成功即发布译文与报告。"""
    main = _load_main(monkeypatch, tmp_path)
    paper_id = _add_paper(main, "job.pdf")
    _write_blocks(main, paper_id)

    async def fake_engine(pid, job, cfg, all_blocks, targets, manifest):
        for index, block in targets:
            all_blocks[index]["translated"] = "中文译文"
        main.store.write_blocks(pid, all_blocks)
        job["translated_ids"] = [b["id"] for _, b in targets]
        job["done"] = len(targets)
        main._flush_job(pid, job, all_blocks)
        return True

    monkeypatch.setattr(main.babeldoc_engine, "available", lambda lang=None: True)
    monkeypatch.setattr(main, "_try_engine_translation", fake_engine)

    job = main.new_job(paper_id, "zh-Hans", total=2)
    main._remember_job(job)
    asyncio.run(
        main._run_translation_job(paper_id, job["job_id"], main.LLMConfigIn(**CONFIG))
    )

    done = main._job_by_id(job["job_id"])
    assert done["status"] == "succeeded", done
    assert done["done"] == 2
    assert done["published_ids"] == ["b1", "b2"]
    assert done["report"]["status"] == "succeeded"

    stored = main.store.read_blocks(paper_id)
    assert all(
        "中文译文" in block["translated"] for block in stored if block["id"] in {"b1", "b2"}
    )


def test_engine_failure_pauses_then_resume(monkeypatch, tmp_path) -> None:
    """引擎失败 → 任务暂停；续跑后引擎成功 → 任务完成。"""
    main = _load_main(monkeypatch, tmp_path)
    paper_id = _add_paper(main, "resume.pdf")
    _write_blocks(main, paper_id)

    calls = {"n": 0}

    async def flaky_engine(pid, job, cfg, all_blocks, targets, manifest):
        calls["n"] += 1
        if calls["n"] == 1:
            return False
        for index, block in targets:
            all_blocks[index]["translated"] = "中文译文"
        main.store.write_blocks(pid, all_blocks)
        job["translated_ids"] = [b["id"] for _, b in targets]
        job["done"] = len(targets)
        main._flush_job(pid, job, all_blocks)
        return True

    monkeypatch.setattr(main.babeldoc_engine, "available", lambda lang=None: True)
    monkeypatch.setattr(main, "_try_engine_translation", flaky_engine)

    job = main.new_job(paper_id, "zh-Hans", total=2)
    main._remember_job(job)
    asyncio.run(
        main._run_translation_job(paper_id, job["job_id"], main.LLMConfigIn(**CONFIG))
    )
    failed = main._job_by_id(job["job_id"])
    assert failed["status"] in {"paused", "failed"}
    assert failed["error"]["code"] == "engine_failed"
    assert failed["resume_available"] is True

    asyncio.run(
        main._run_translation_job(paper_id, job["job_id"], main.LLMConfigIn(**CONFIG))
    )
    done = main._job_by_id(job["job_id"])
    assert done["status"] == "succeeded", done
    assert done["done"] == 2


def test_job_file_recovers_as_paused(monkeypatch, tmp_path) -> None:
    main = _load_main(monkeypatch, tmp_path)
    paper_id = _add_paper(main, "restart.pdf")
    job = main.new_job(paper_id, "zh-Hans", total=3)
    job["status"] = "running"
    job["done"] = 1
    job["translated_ids"] = ["b1"]
    job["published_ids"] = ["b1"]
    main.write_translation_job(main.DATA_DIR, job)

    main._JOB_CACHE.clear()
    main._recover_translation_jobs()
    recovered = main._job_by_id(job["job_id"])
    assert recovered is not None
    assert recovered["status"] == "paused"
    assert recovered["resume_available"] is True
    assert recovered["done"] == 1
    assert json.loads(
        (main.DATA_DIR / "papers" / f"{paper_id}_translated" / "zh-Hans" / "translation_job.json").read_text(
            encoding="utf-8"
        )
    )["status"] == "paused"


def test_cancel_paused_job(monkeypatch, tmp_path) -> None:
    main = _load_main(monkeypatch, tmp_path)
    paper_id = _add_paper(main, "cancel.pdf")
    job = main.new_job(paper_id, "zh-Hans", total=2)
    job["status"] = "paused"
    job["done"] = 1
    job["translated_ids"] = ["b1"]
    job["published_ids"] = ["b1"]
    job["resume_available"] = True
    main.write_translation_job(main.DATA_DIR, job)
    main._remember_job(job)

    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        res = client.post(
            f"/api/papers/{paper_id}/translation/jobs/{job['job_id']}/cancel"
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["status"] == "cancelled"
        assert body["resume_available"] is False


def test_full_translation_requires_engine(monkeypatch, tmp_path) -> None:
    """整篇全局翻译只走外部引擎：引擎没就绪时直接报错，不再静默回退分块。"""
    main = _load_main(monkeypatch, tmp_path)
    paper_id = _add_paper(main, "engine-missing.pdf")
    _write_blocks(main, paper_id)

    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        res = client.post(
            f"/api/papers/{paper_id}/translation/jobs",
            json={"config": CONFIG, "target_lang": "zh-Hans"},
        )
        assert res.status_code == 409, res.text
        assert res.headers.get("x-translation-code") == "engine_missing"
        assert "引擎" in res.json()["detail"]


def test_engine_failure_does_not_fall_back_to_block_translation(
    monkeypatch, tmp_path
) -> None:
    """引擎运行失败时任务显式失败；不允许再走分块翻译 + 内部回写器。"""
    main = _load_main(monkeypatch, tmp_path)
    paper_id = _add_paper(main, "engine-failed.pdf")
    _write_blocks(main, paper_id)

    calls = {"translate": 0}

    async def fake_glossary(*args, **kwargs):
        return {}

    async def fake_translate(*args, **kwargs):
        calls["translate"] += 1
        return "中文译文"

    async def boom(*args, **kwargs):
        raise main.babeldoc_engine.EngineError("boom")

    monkeypatch.setattr(main, "build_glossary", fake_glossary)
    monkeypatch.setattr(main, "translate_block", fake_translate)
    monkeypatch.setattr(main.babeldoc_engine, "available", lambda lang=None: True)
    monkeypatch.setattr(main.babeldoc_engine, "run", boom)

    job = main.new_job(paper_id, "zh-Hans", total=2)
    main.write_translation_job(main.DATA_DIR, job)
    main._remember_job(job)

    asyncio.run(
        main._run_translation_job(
            paper_id,
            job["job_id"],
            main.LLMConfigIn(**CONFIG),
        )
    )

    done = main._job_by_id(job["job_id"])
    assert done["status"] in {"paused", "failed"}
    assert done["error"]["code"] == "engine_failed"
    assert calls["translate"] == 0
    assert done["done"] == 0
