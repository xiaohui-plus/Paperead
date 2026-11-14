"""公式批量补识别（已有论文）与识别结果清理的测试。"""

from __future__ import annotations

import importlib

from fastapi.testclient import TestClient

from paperead.formula import _strip_number

PDF_LIKE = b"%PDF-1.4\n" + b"x" * 400
PNG_LIKE = b"\x89PNG\r\n\x1a\n" + b"x" * 400


def test_strip_number_removes_trailing_noise():
    assert _strip_number(r"s_{ij}=q_i \quad (5)") == r"s_{ij}=q_i"
    assert _strip_number(r"E=mc^2,\qquad(2.1)") == r"E=mc^2"
    assert _strip_number(r"a=b") == r"a=b"


def test_batch_recognize_persists_latex(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPEREAD_TOKEN", "batch-token")
    monkeypatch.setenv("PAPEREAD_DATA", str(tmp_path / "lib"))
    monkeypatch.setenv("PAPEREAD_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("PAPEREAD_MOCK", "0")

    import server.main as main

    importlib.reload(main)
    client = TestClient(main.app)
    headers = {"X-Paperead-Token": "batch-token"}

    row = main.store.add("batch.pdf", PDF_LIKE, None)
    paper_id = row["id"]
    asset = (
        main.store.papers_dir
        / f"{paper_id}_assets"
        / paper_id
        / "equations"
        / "eq_0001.png"
    )
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(PNG_LIKE)
    main.store.write_blocks(
        paper_id,
        [
            {
                "id": "b0001",
                "kind": "equation",
                "latex": "",
                "eq_number": "5",
                "image_path": f"/api/papers/{paper_id}/asset/{paper_id}/equations/eq_0001.png",
            },
            {"id": "b0002", "kind": "para", "text": "keep", "latex": ""},
        ],
    )

    async def fake_recognize(image, model=None, backend=None):
        return r"a_{ij}=\frac{q_i\cdot k_j^\top}{\sqrt{d}}"

    monkeypatch.setattr(main, "recognize_latex", fake_recognize)

    payload = {
        "config": {
            "api_key": "test-key-123",
            "base_url": "https://example.com/v1",
            "model": "vision-test",
            "timeout": 10,
        }
    }
    with client:
        first = client.post(
            f"/api/papers/{paper_id}/formulas/recognize", headers=headers, json=payload
        )
        assert first.status_code == 200, first.text
        body = first.json()
        assert body["recognized"] == 1 and body["total"] == 1
        assert body["items"][0]["block_id"] == "b0001"

        blocks = main.store.read_blocks(paper_id)
        assert blocks[0]["latex"] == r"a_{ij}=\frac{q_i\cdot k_j^\top}{\sqrt{d}}"
        assert blocks[0]["eq_number"] == "5"
        assert blocks[1]["text"] == "keep"

        # 已经识别过的公式不再重复调用
        second = client.post(
            f"/api/papers/{paper_id}/formulas/recognize", headers=headers, json=payload
        )
        assert second.status_code == 200
        assert second.json()["total"] == 0


def test_batch_recognize_requires_vision_config(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPEREAD_TOKEN", "batch-token")
    monkeypatch.setenv("PAPEREAD_DATA", str(tmp_path / "lib"))
    monkeypatch.setenv("PAPEREAD_CACHE", str(tmp_path / "cache"))

    import server.main as main

    importlib.reload(main)
    main._vision_config = None
    client = TestClient(main.app)
    headers = {"X-Paperead-Token": "batch-token"}
    row = main.store.add("batch2.pdf", PDF_LIKE, None)

    with client:
        res = client.post(
            f"/api/papers/{row['id']}/formulas/recognize", headers=headers, json={}
        )
    assert res.status_code == 400
    assert "识图模型" in res.json()["detail"]