import importlib

import pymupdf
from fastapi.testclient import TestClient


def _make_pdf(path):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "A Test Paper", fontsize=20)
    for i in range(40):
        page.insert_text(
            (72, 110 + i * 12),
            f"Line {i}: the quick brown fox jumps over the lazy dog.",
            fontsize=9,
        )
    doc.save(path)
    doc.close()


def test_import_from_local_path(monkeypatch, tmp_path):
    pdf = tmp_path / "论文 测试.pdf"
    _make_pdf(str(pdf))
    assert pdf.stat().st_size >= 1024

    monkeypatch.setenv("PAPER_REFLOW_TOKEN", "import-token")
    monkeypatch.setenv("PAPER_REFLOW_PORT", "8124")
    monkeypatch.setenv("PAPER_REFLOW_DATA", str(tmp_path / "lib"))
    monkeypatch.setenv("PAPER_REFLOW_MOCK", "1")

    import server.main as main

    importlib.reload(main)

    with TestClient(main.app) as client:
        assert client.get("/").status_code == 200
        res = client.post(
            "/api/papers/import",
            json={"path": str(pdf)},
            headers={"X-Paper-Reflow-Token": "import-token"},
        )
        assert res.status_code == 200, res.text
        added = res.json()["added"]
        assert len(added) == 1
        assert added[0]["filename"] == pdf.name
        assert (tmp_path / "lib" / "papers" / f"{added[0]['id']}.pdf").is_file()
