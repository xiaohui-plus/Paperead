"""外部论文笔记导入：front-matter、小节归属与检索。"""

from __future__ import annotations

from pathlib import Path

from paperead.notes_import import collect_notes, parse_note
from server.notes_search import NoteSearch
from server.notes_store import NoteStore

NOTE = """---
title: >-
  [论文解读] Agile Deliberation: Concept Deliberation
description: >-
  [CVPR 2026][Reasoning] 一句摘要。
tags:
  - "CVPR 2026"
  - "Reasoning"
  - "概念审议"
---

# Agile Deliberation

**会议**: CVPR 2026

## 一句话总结
主观概念的人在回路框架。

## 方法详解

### 整体框架
给定图像空间 $X$，构造定义 $d$。

```mermaid
flowchart TD
    A --> B
```

### 关键设计
$$d_{t+1}=\\arg\\max F1$$

用字典学习学一组基 $W$。

## 实验关键数据

| 概念 | F1 |
|------|----|
| paid | 0.48 |

## 局限性
样本量小。

<!-- RELATED:START -->

<div class="related-papers" markdown="1">

## 相关论文

- [另一篇](../../ACL2025/x.md)

</div>

<!-- RELATED:END -->
"""


def _write(root: Path, rel: str = "docs/CVPR2026/llm_reasoning/demo.md") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(NOTE, encoding="utf-8")
    return path


def test_front_matter_and_location(tmp_path: Path) -> None:
    path = _write(tmp_path)
    note = parse_note(path, root=tmp_path / "docs")

    assert note.title == "Agile Deliberation: Concept Deliberation"
    assert note.description == "[CVPR 2026][Reasoning] 一句摘要。"
    assert note.tags == ["CVPR 2026", "Reasoning", "概念审议"]
    assert note.conference == "CVPR2026"
    assert note.topic == "llm_reasoning"


def test_h3_does_not_steal_section(tmp_path: Path) -> None:
    note = parse_note(_write(tmp_path), root=tmp_path / "docs")
    sections = note.sections

    # 「方法详解」下的内容挂在 H3 子标题里，必须仍归属 H2 小节。
    assert "构造定义" in sections["method"]
    assert "字典学习" in sections["method"]
    assert sections["method"].count("整体框架") == 0
    assert "主观概念的人" in sections["oneliner"]


def test_block_kinds_and_noise(tmp_path: Path) -> None:
    note = parse_note(_write(tmp_path), root=tmp_path / "docs")
    kinds = [block.kind for block in note.blocks]

    assert kinds[0] == "title"
    assert "heading" in kinds
    assert "table" in kinds
    assert "equation" in kinds
    table = next(block for block in note.blocks if block.kind == "table")
    assert table.translatable is False
    assert "| 概念 | F1 |" in table.text

    texts = " ".join(block.text for block in note.blocks)
    assert "related-papers" not in texts
    assert "RELATED:START" not in texts
    assert "flowchart TD" in texts


def test_store_is_idempotent(tmp_path: Path) -> None:
    note = parse_note(_write(tmp_path), root=tmp_path / "docs")
    store = NoteStore(tmp_path / "data")

    first = store.upsert(note)
    second = store.upsert(note)

    assert first["id"] == second["id"]
    assert store.count() == 1
    assert first["license"] == "CC BY-NC-SA 4.0"
    assert first["origin"].endswith("M0rtzz/paper-notes")


def test_search_hits_title_and_body(tmp_path: Path) -> None:
    note = parse_note(_write(tmp_path), root=tmp_path / "docs")
    store = NoteStore(tmp_path / "data")
    store.upsert(note)
    search = NoteSearch(store, tmp_path / "data" / "notes" / "search.db")

    assert search.search("Agile Deliberation")
    # 只出现在正文里的词也要能召回。
    assert search.search("字典学习")
    # 两字中文走 LIKE 兜底，不能报 SQL 转义错误。
    assert search.search("框架")
    assert search.search("完全不相关的词组") == []


def test_collect_skips_index(tmp_path: Path) -> None:
    _write(tmp_path)
    (tmp_path / "docs" / "CVPR2026" / "llm_reasoning" / "index.md").write_text(
        "# 索引", encoding="utf-8"
    )

    notes = collect_notes(tmp_path / "docs")

    assert len(notes) == 1
    assert notes[0].slug == "demo"