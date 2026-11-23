"""行内公式批量识图：写回 latex、不可编译拒绝、sha1 缓存命中。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pymupdf

from paperead.formula import recognize_inline_math
from paperead.layout import Block


class FakeBackend:
    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls = 0

    async def chat(self, messages, **kwargs):  # noqa: ANN001
        self.calls += 1
        return self.payload


def _pdf(path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=200)
    page.insert_text((20, 30), "alpha", fontsize=10)
    doc.save(path)
    doc.close()


def _block() -> Block:
    return Block(
        id="b0001",
        order=0,
        page=1,
        kind="para",
        text="see alpha",
        bbox=(10, 10, 280, 60),
        inline_math=[
            {"start": 4, "end": 9, "source": "alpha", "bbox": [15, 15, 80, 42]}
        ],
    )


def test_inline_math_latex_written(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PAPEREAD_CACHE", str(tmp_path / "cache"))
    pdf = tmp_path / "p.pdf"
    _pdf(pdf)
    block = _block()
    backend = FakeBackend(r"\alpha")

    stats = asyncio.run(
        recognize_inline_math(pdf, [block], backend=backend)
    )

    assert stats["ok"] == 1
    assert block.inline_math[0]["latex"] == r"\alpha"


def test_uncompilable_latex_rejected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PAPEREAD_CACHE", str(tmp_path / "cache"))
    pdf = tmp_path / "p.pdf"
    _pdf(pdf)
    block = _block()

    stats = asyncio.run(
        recognize_inline_math(pdf, [block], backend=FakeBackend(r"\frac{"))
    )

    assert stats["failed"] == 1
    assert not block.inline_math[0].get("latex")


def test_second_run_hits_cache(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PAPEREAD_CACHE", str(tmp_path / "cache"))
    pdf = tmp_path / "p.pdf"
    _pdf(pdf)
    backend = FakeBackend(r"\beta")

    first = _block()
    stats1 = asyncio.run(recognize_inline_math(pdf, [first], backend=backend))
    assert stats1["ok"] == 1

    second = _block()
    stats2 = asyncio.run(recognize_inline_math(pdf, [second], backend=backend))
    assert stats2["cached"] == 1
    assert second.inline_math[0]["latex"] == r"\beta"
    assert backend.calls == 1