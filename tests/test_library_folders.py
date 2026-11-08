from __future__ import annotations

import json
from pathlib import Path

import pytest

from server.store import (
    FolderExistsError,
    FolderNameError,
    LibraryStore,
)


def _paper(
    paper_id: str,
    *,
    folder_id: str | None = None,
    project_id: str | None = None,
) -> dict:
    row = {
        "id": paper_id,
        "title": f"Paper {paper_id}",
        "authors": "",
        "filename": f"{paper_id}.pdf",
        "pages": 1,
        "size": 1024,
        "created_at": 1,
        "status": "ready",
        "error": "",
        "translated": 0,
        "blocks": 0,
    }
    if folder_id:
        row["folder_id"] = folder_id
    if project_id:
        row["project_id"] = project_id
    return row


def _seed(store: LibraryStore, rows: list[dict]) -> None:
    store.index.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")


def test_folder_lifecycle_and_counts(tmp_path: Path):
    store = LibraryStore(tmp_path / "lib")
    _seed(store, [_paper("p1"), _paper("p2")])

    folder = store.create_folder("免疫治疗")
    assert folder["name"] == "免疫治疗"
    assert folder["paper_count"] == 0
    assert store.list_folders()[0]["paper_count"] == 0

    moved = store.move_paper("p1", folder["id"])
    assert moved is not None
    assert moved["folder_id"] == folder["id"]
    assert store.list_folders()[0]["paper_count"] == 1

    renamed = store.rename_folder(folder["id"], "肿瘤免疫")
    assert renamed is not None
    assert renamed["name"] == "肿瘤免疫"
    assert renamed["paper_count"] == 1

    deleted = store.delete_folder(folder["id"])
    assert deleted == {"ok": True, "moved": 1}
    assert store.list_folders() == []
    assert store.get("p1") is not None
    assert store.get("p1").get("folder_id") is None


def test_folder_name_validation_and_duplicate(tmp_path: Path):
    store = LibraryStore(tmp_path / "lib")

    with pytest.raises(FolderNameError):
        store.create_folder("   ")
    with pytest.raises(FolderNameError):
        store.create_folder("x" * 41)

    first = store.create_folder("免疫治疗")
    with pytest.raises(FolderExistsError):
        store.create_folder("  免疫治疗  ")

    same = store.rename_folder(first["id"], "免疫治疗")
    assert same is not None and same["name"] == "免疫治疗"

    other = store.create_folder("CRISPR")
    with pytest.raises(FolderExistsError):
        store.rename_folder(other["id"], "免疫治疗")


def test_move_to_missing_folder_and_missing_paper(tmp_path: Path):
    store = LibraryStore(tmp_path / "lib")
    _seed(store, [_paper("p1")])

    with pytest.raises(LookupError):
        store.move_paper("p1", "f_missing")
    assert store.move_paper("missing", None) is None


def test_project_papers_do_not_count_in_folders(tmp_path: Path):
    store = LibraryStore(tmp_path / "lib")
    folder = store.create_folder("免疫治疗")
    _seed(
        store,
        [
            _paper("p1", folder_id=folder["id"]),
            _paper("p2", folder_id=folder["id"], project_id="pr_1"),
        ],
    )

    folders = store.list_folders()
    assert folders[0]["paper_count"] == 1
    assert [row["id"] for row in store.list()] == ["p1"]
