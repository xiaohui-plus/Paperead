from pathlib import Path

from server.research.tools_api import _read_full_text, _read_text, _read_text_window


def test_read_text_window_returns_next_chunk(tmp_path: Path):
    body = "甲" * 25000
    path = tmp_path / "review.md"
    path.write_text(body, encoding="utf-8")

    first, total = _read_text_window(path, 0, 20000)
    assert total == 25000
    assert first == body[:20000]

    second, _ = _read_text_window(path, 20000, 20000)
    assert second == body[20000:]


def test_full_read_keeps_long_draft_whole(tmp_path: Path):
    body = "乙" * 30000
    path = tmp_path / "review.md"
    path.write_text(body, encoding="utf-8")

    assert _read_text(path) == body[:20000] + "\n…（已截断）"
    assert _read_full_text(path) == body
