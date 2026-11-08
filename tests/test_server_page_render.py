"""原版 PDF 页图接口的测试。"""

from __future__ import annotations

import importlib

import pymupdf
from fastapi.testclient import TestClient

from server.store import LibraryStore

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def make_pdf(path, pages: int = 2) -> None:
    doc = pymupdf.open()
    for index in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"Page {index + 1}", fontsize=20)
        page.insert_text((72, 110), "Body text for the page rendering test.", fontsize=10)
    doc.save(path)
    doc.close()


def boot(monkeypatch, tmp_path, token: str = "page-token"):
    monkeypatch.setenv("PAPER_REFLOW_TOKEN", token)
    monkeypatch.setenv("PAPER_REFLOW_PORT", "8125")
    monkeypatch.setenv("PAPER_REFLOW_DATA", str(tmp_path / "lib"))
    monkeypatch.setenv("PAPER_REFLOW_MOCK", "1")

    import server.main as main

    importlib.reload(main)
    return main, TestClient(main.app), token


def test_render_page_endpoint_caches_png(monkeypatch, tmp_path):
    pdf = tmp_path / "sample.pdf"
    make_pdf(pdf, pages=2)
    _main, client, token = boot(monkeypatch, tmp_path)
    headers = {"X-Paper-Reflow-Token": token}

    with client:
        added = client.post("/api/papers/import", json={"path": str(pdf)}, headers=headers)
        assert added.status_code == 200, added.text
        paper_id = added.json()["added"][0]["id"]

        page = client.get(f"/api/papers/{paper_id}/page/1.png?width=600", headers=headers)
        assert page.status_code == 200, page.text
        assert page.headers["content-type"] == "image/png"
        assert page.content.startswith(PNG_MAGIC)

        cached = tmp_path / "lib" / "papers" / f"{paper_id}_pages" / "p0001@600.png"
        assert cached.is_file()
        # 第二次直接命中缓存，内容一致
        again = client.get(f"/api/papers/{paper_id}/page/1.png?width=600", headers=headers)
        assert again.content == page.content


def test_render_page_clamps_width_and_rejects_bad_pages(monkeypatch, tmp_path):
    pdf = tmp_path / "sample.pdf"
    make_pdf(pdf, pages=2)
    _main, client, token = boot(monkeypatch, tmp_path)
    headers = {"X-Paper-Reflow-Token": token}

    with client:
        paper_id = client.post(
            "/api/papers/import", json={"path": str(pdf)}, headers=headers
        ).json()["added"][0]["id"]

        # 宽度夹到上限 1600
        wide = client.get(f"/api/papers/{paper_id}/page/1.png?width=99999", headers=headers)
        assert wide.status_code == 200
        assert (tmp_path / "lib" / "papers" / f"{paper_id}_pages" / "p0001@1600.png").is_file()

        assert (
            client.get(f"/api/papers/{paper_id}/page/9.png", headers=headers).status_code == 404
        )
        assert (
            client.get(f"/api/papers/{paper_id}/page/0.png", headers=headers).status_code == 404
        )
        assert (
            client.get("/api/papers/not-a-paper/page/1.png", headers=headers).status_code == 404
        )


def test_remove_clears_page_cache(tmp_path):
    pdf = tmp_path / "sample.pdf"
    make_pdf(pdf, pages=1)
    store = LibraryStore(tmp_path / "lib")
    row = store.add("sample.pdf", pdf.read_bytes())

    assert store.render_page(row["id"], 1, 600)
    assert store.page_dir(row["id"]).is_dir()

    assert store.remove(row["id"])
    assert not store.page_dir(row["id"]).exists()
