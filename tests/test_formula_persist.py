"""公式按需识别的持久化测试。

覆盖：
- 识别结果按图片 sha1 缓存，重复识别不再调模型
- 阅读页按需识别的 LaTeX 回写到 blocks.json，刷新后直接走 KaTeX
"""

from __future__ import annotations

import importlib

from fastapi.testclient import TestClient

from paperead.formula import read_cached_latex, write_cached_latex
from server.store import LibraryStore

PDF_LIKE = b"\x89PNG\r\n\x1a\n" + b"x" * 400


def test_formula_cache_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPEREAD_CACHE", str(tmp_path / "cache"))
    assert read_cached_latex(PDF_LIKE) == ""
    write_cached_latex(PDF_LIKE, r"S_{\mathrm{AA}}[u,v]")
    assert read_cached_latex(PDF_LIKE) == r"S_{\mathrm{AA}}[u,v]"


def test_update_block_latex_keeps_other_fields(tmp_path):
    store = LibraryStore(tmp_path / "lib")
    paper_id = "p_test"
    store.write_blocks(
        paper_id,
        [
            {"id": "b0000", "kind": "para", "text": "hello", "latex": ""},
            {"id": "b0001", "kind": "equation", "latex": "", "eq_number": "3"},
        ],
    )

    assert store.update_block_latex(paper_id, "b0001", r"H^{(l+1)}=\sigma(\tilde{A}H^{(l)}W^{(l)})")
    blocks = store.read_blocks(paper_id)
    assert blocks[0]["text"] == "hello"
    assert blocks[1]["latex"].startswith("H^{(l+1)}")
    assert blocks[1]["eq_number"] == "3"

    # 不存在的块/空 latex 都不应改动文件
    assert store.update_block_latex(paper_id, "nope", "x") is False
    assert store.update_block_latex(paper_id, "b0001", "") is False


def test_formula_endpoint_persists_latex(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPEREAD_TOKEN", "formula-token")
    monkeypatch.setenv("PAPEREAD_PORT", "8126")
    monkeypatch.setenv("PAPEREAD_DATA", str(tmp_path / "lib"))
    monkeypatch.setenv("PAPEREAD_MOCK", "1")

    import server.main as main

    importlib.reload(main)
    client = TestClient(main.app)
    headers = {"X-Paperead-Token": "formula-token"}
    paper_id = "p_formula"
    main.store.write_blocks(
        paper_id, [{"id": "b0007", "kind": "equation", "latex": "", "image_path": None}]
    )

    with client:
        res = client.post(
            "/api/formula",
            headers=headers,
            json={
                "image_base64": "aGVsbG8=",
                "paper_id": paper_id,
                "block_id": "b0007",
                "config": {
                    "api_key": "test-key-123",
                    "base_url": "https://example.com/v1",
                    "model": "vision-test",
                    "timeout": 10,
                },
            },
        )
        assert res.status_code == 200, res.text
        assert res.json()["latex"] == main.MOCK_LATEX

    blocks = main.store.read_blocks(paper_id)
    assert blocks[0]["latex"] == main.MOCK_LATEX