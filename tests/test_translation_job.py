"""Phase 1：服务端持久化翻译任务的启动、进度、失败与续跑。"""

from __future__ import annotations

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
    main = _load_main(monkeypatch, tmp_path)
    paper_id = _add_paper(main)
    _write_blocks(main, paper_id)

    async def fake_glossary(*args, **kwargs):
        return {}

    monkeypatch.setattr(main, "build_glossary", fake_glossary)
    monkeypatch.setattr(main, "translate_block", _fake_translate(""))

    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        started = client.post(
            f"/api/papers/{paper_id}/translation/jobs",
            json={
                "config": CONFIG,
                "target_lang": "zh-Hans",
                "auto_highlights": False,
            },
        )
        assert started.status_code == 200, started.text
        job_id = started.json()["job_id"]
        done = _wait_job(client, paper_id, job_id)

        assert done["status"] == "succeeded", done
        assert done["total"] == 2
        assert done["done"] == 2
        assert done["report"]["coverage"] == 1.0
        assert len(done["blocks"]) == 2
        assert all("中文译文" in block["translated"] for block in done["blocks"])

        report_res = client.get(f"/api/papers/{paper_id}/translation/report")
        assert report_res.status_code == 200
        assert report_res.json()["status"] == "succeeded"

        manifest = client.get(f"/api/papers/{paper_id}/translation/manifest").json()
        assert manifest["translated"] == 2
        assert manifest["eligible_total"] == 2

        # 断线重连：since=0 一次补回全部已发布块；游标之后不再重复返回。
        replay = client.get(f"/api/papers/{paper_id}/translation/jobs/{job_id}?since=0").json()
        assert len(replay["blocks"]) == 2
        assert replay["next_cursor"] == 2
        tail = client.get(f"/api/papers/{paper_id}/translation/jobs/{job_id}?since=2").json()
        assert tail["blocks"] == []
        assert tail["next_cursor"] == 2


def test_auth_failure_pauses_then_resume(monkeypatch, tmp_path) -> None:
    main = _load_main(monkeypatch, tmp_path)
    paper_id = _add_paper(main, "auth.pdf")
    _write_blocks(main, paper_id)

    async def fake_glossary(*args, **kwargs):
        return {}

    async def deny(block, glossary, prev_tail, next_head, **kwargs):
        raise AuthenticationError("401 invalid api key")

    monkeypatch.setattr(main, "build_glossary", fake_glossary)
    monkeypatch.setattr(main, "translate_block", deny)

    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        started = client.post(
            f"/api/papers/{paper_id}/translation/jobs",
            json={"config": CONFIG, "target_lang": "zh-Hans"},
        )
        job_id = started.json()["job_id"]
        failed = _wait_job(client, paper_id, job_id)
        assert failed["status"] == "failed"
        assert failed["error"]["code"] == "auth"
        assert failed["resume_available"] is True

        monkeypatch.setattr(main, "translate_block", _fake_translate(""))
        resumed = client.post(
            f"/api/papers/{paper_id}/translation/jobs/{job_id}/resume",
            json={"config": CONFIG},
        )
        assert resumed.status_code == 200, resumed.text
        done = _wait_job(client, paper_id, job_id)
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
