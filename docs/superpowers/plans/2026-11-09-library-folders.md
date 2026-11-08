# 文献库文件夹管理 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给文献库增加单层文件夹：新建 / 重命名 / 删除、拖拽移入、0.7 秒左边缘移出、空/非空文件夹不同图标与状态。

**Architecture:** 后端在 `data/folders.json` 存文件夹，论文行加可选 `folder_id`；前端侧栏 `LibraryFolders` 负责 CRUD 与投放目标，`LibraryPage` 负责过滤、乐观更新和 0.7 秒左边缘投放条。拖拽用原生 HTML5 DnD，拖拽期间把源卡片钉在当前网格，避免切视图导致拖拽中断。

**Tech Stack:** FastAPI、Pydantic、原生 JSON 存储、React 19、TypeScript、HTML5 Drag and Drop、pytest、Playwright（手工验收）。

---

## 文件结构

| 文件 | 责任 |
|---|---|
| `server/store.py` | 文件夹读写、计数、创建/改名/删除、论文移入/移出 |
| `server/main.py` | 文件夹 REST 接口、论文移动接口、上传带 `folder_id` |
| `tests/test_library_folders.py` | Store + API 回归 |
| `web/src/lib/library.ts` | `Folder` 类型与文件夹 API 客户端 |
| `web/src/components/icons.tsx` | 新增 `IconFolderPapers` |
| `web/src/components/LibraryFolders.tsx` | 侧栏文件夹区、创建/改名/删除、拖拽投放 |
| `web/src/components/Sidebar.tsx` | 支持传入文件夹区 |
| `web/src/components/PaperCard.tsx` | 论文卡可拖拽、拖拽态 |
| `web/src/components/LibraryPage.tsx` | 文件夹状态、过滤、上传归属、拖拽编排、左边缘投放条 |
| `web/src/styles.css` | 文件夹行、菜单、图标态、拖拽态、投放条、动画 |
| `README.md` | 文献库能力补一条「文件夹管理」 |

---

## Task 1: Store 文件夹数据层

**Files:**
- Modify: `server/store.py`
- Test: `tests/test_library_folders.py`（新建）

- [ ] **Step 1: 写失败测试**

创建 `tests/test_library_folders.py`：

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_library_folders.py -q`

Expected: FAIL，`cannot import name 'FolderExistsError'` 或 `LibraryStore` 没有 `create_folder`。

- [ ] **Step 3: 实现 Store 方法**

在 `server/store.py` 顶部、`log` 后面加异常：

```python
class FolderNameError(ValueError):
    """文件夹名称非法。"""


class FolderExistsError(ValueError):
    """同名文件夹已存在。"""
```

在 `LibraryStore.__init__` 里、`self.index` 后加：

```python
        self.folders_path = root / "folders.json"
```

在 `update` 方法后加文件夹实现：

```python
    # ── 文件夹 ──────────────────────────────────────────

    def _read_folders(self) -> list[dict[str, Any]]:
        if not self.folders_path.exists():
            return []
        try:
            payload = json.loads(self.folders_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        if isinstance(payload, dict):
            rows = payload.get("folders") or []
        elif isinstance(payload, list):
            rows = payload
        else:
            rows = []
        return [
            row
            for row in rows
            if isinstance(row, dict) and row.get("id") and row.get("name")
        ]

    def _write_folders(self, folders: list[dict[str, Any]]) -> None:
        self.folders_path.write_text(
            json.dumps(
                {"version": 1, "folders": folders},
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )

    @staticmethod
    def _clean_folder_name(name: str) -> str:
        value = re.sub(r"\s+", " ", (name or "").strip())
        if not value:
            raise FolderNameError("文件夹名称不能为空")
        if len(value) > 40:
            raise FolderNameError("文件夹名称不能超过 40 个字")
        return value

    @staticmethod
    def _folder_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in rows:
            if row.get("project_id"):
                continue
            folder_id = row.get("folder_id")
            if folder_id:
                counts[str(folder_id)] = counts.get(str(folder_id), 0) + 1
        return counts

    def list_folders(self) -> list[dict[str, Any]]:
        with self._lock:
            folders = self._read_folders()
            counts = self._folder_counts(self._read())
        folders.sort(key=lambda row: (row.get("created_at", 0), row["id"]))
        return [
            {**row, "paper_count": counts.get(row["id"], 0)}
            for row in folders
        ]

    def get_folder(self, folder_id: str) -> dict[str, Any] | None:
        return next(
            (row for row in self._read_folders() if row["id"] == folder_id),
            None,
        )

    def create_folder(self, name: str) -> dict[str, Any]:
        cleaned = self._clean_folder_name(name)
        with self._lock:
            folders = self._read_folders()
            if any(row["name"].casefold() == cleaned.casefold() for row in folders):
                raise FolderExistsError("文件夹名称已存在")
            folder = {
                "id": f"f_{uuid.uuid4().hex[:12]}",
                "name": cleaned,
                "created_at": time.time(),
            }
            folders.append(folder)
            self._write_folders(folders)
        return {**folder, "paper_count": 0}

    def rename_folder(self, folder_id: str, name: str) -> dict[str, Any] | None:
        cleaned = self._clean_folder_name(name)
        with self._lock:
            folders = self._read_folders()
            target = next(
                (row for row in folders if row["id"] == folder_id),
                None,
            )
            if target is None:
                return None
            if any(
                row["id"] != folder_id
                and row["name"].casefold() == cleaned.casefold()
                for row in folders
            ):
                raise FolderExistsError("文件夹名称已存在")
            target["name"] = cleaned
            self._write_folders(folders)
            counts = self._folder_counts(self._read())
        return {**target, "paper_count": counts.get(folder_id, 0)}

    def delete_folder(self, folder_id: str) -> dict[str, Any] | None:
        with self._lock:
            folders = self._read_folders()
            if not any(row["id"] == folder_id for row in folders):
                return None
            rows = self._read()
            moved = 0
            for row in rows:
                if row.get("folder_id") == folder_id:
                    row.pop("folder_id", None)
                    moved += 1
            self._write(rows)
            self._write_folders(
                [row for row in folders if row["id"] != folder_id]
            )
        return {"ok": True, "moved": moved}

    def move_paper(
        self, paper_id: str, folder_id: str | None
    ) -> dict[str, Any] | None:
        with self._lock:
            rows = self._read()
            paper = next(
                (row for row in rows if row["id"] == paper_id),
                None,
            )
            if paper is None:
                return None
            if folder_id and not any(
                row["id"] == folder_id for row in self._read_folders()
            ):
                raise LookupError("文件夹不存在")
            if folder_id:
                paper["folder_id"] = folder_id
            else:
                paper.pop("folder_id", None)
            self._write(rows)
        return paper
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_library_folders.py -q`

Expected: PASS（4 passed）。

- [ ] **Step 5: 提交**

```bash
git add server/store.py tests/test_library_folders.py
git commit -m "feat(library): add folder storage"
```

---

## Task 2: 文件夹 API 与上传归属

**Files:**
- Modify: `server/main.py`
- Test: `tests/test_library_folders.py`

- [ ] **Step 1: 追加失败 API 测试**

在 `tests/test_library_folders.py` 末尾加：

```python
from fastapi.testclient import TestClient

import server.main as main


@pytest.fixture()
def api(tmp_path: Path, monkeypatch):
    store = LibraryStore(tmp_path / "api-lib")
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "TOKEN", "")

    async def _noop(_paper_id: str) -> None:
        return None

    monkeypatch.setattr(main, "_parse_task", _noop)
    with TestClient(main.app) as client:
        yield client, store


def test_api_folder_crud_and_paper_move(api):
    client, store = api
    _seed(store, [_paper("p1")])

    created = client.post("/api/folders", json={"name": "免疫治疗"})
    assert created.status_code == 201
    folder = created.json()
    assert folder["paper_count"] == 0

    listed = client.get("/api/folders")
    assert listed.status_code == 200
    assert listed.json()["folders"][0]["id"] == folder["id"]

    renamed = client.patch(
        f"/api/folders/{folder['id']}",
        json={"name": "肿瘤免疫"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "肿瘤免疫"

    moved = client.patch(
        "/api/papers/p1/folder",
        json={"folder_id": folder["id"]},
    )
    assert moved.status_code == 200
    assert moved.json()["folder_id"] == folder["id"]
    assert client.get("/api/papers").json()["papers"][0]["folder_id"] == folder["id"]

    deleted = client.delete(f"/api/folders/{folder['id']}")
    assert deleted.status_code == 200
    assert deleted.json() == {"ok": True, "moved": 1}
    assert client.get("/api/papers").json()["papers"][0].get("folder_id") is None


def test_api_errors(api):
    client, _ = api
    assert client.post("/api/folders", json={"name": "  "}).status_code == 400

    first = client.post("/api/folders", json={"name": "免疫治疗"}).json()
    assert client.post("/api/folders", json={"name": "免疫治疗"}).status_code == 409
    assert client.patch(
        "/api/folders/missing", json={"name": "x"}
    ).status_code == 404
    assert client.delete("/api/folders/missing").status_code == 404
    assert client.patch(
        "/api/papers/missing/folder", json={"folder_id": first["id"]}
    ).status_code == 404
    assert client.patch(
        "/api/papers/p1/folder", json={"folder_id": "f_missing"}
    ).status_code == 404


def test_upload_into_folder(api):
    client, store = api
    folder = client.post("/api/folders", json={"name": "免疫治疗"}).json()
    payload = b"%PDF-1.4\n" + b"x" * 2048

    res = client.post(
        "/api/papers/upload",
        files=[("files", ("paper.pdf", payload, "application/pdf"))],
        data={"folder_id": folder["id"]},
    )
    assert res.status_code == 200
    added = res.json()["added"]
    assert len(added) == 1
    assert added[0]["folder_id"] == folder["id"]
    assert store.get(added[0]["id"])["folder_id"] == folder["id"]

    missing = client.post(
        "/api/papers/upload",
        files=[("files", ("paper.pdf", payload, "application/pdf"))],
        data={"folder_id": "f_missing"},
    )
    assert missing.status_code == 404
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_library_folders.py -q`

Expected: FAIL，`/api/folders` 404。

- [ ] **Step 3: 实现 API**

在 `server/main.py` 的 store 导入处改成：

```python
from server.store import (
    FolderExistsError,
    FolderNameError,
    LibraryStore,
)
```

在 `from fastapi import ...` 一行加入 `Form`：

```python
from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
```

在 `ImportReq` 后加请求模型：

```python
class FolderIn(BaseModel):
    name: str = ""


class PaperFolderIn(BaseModel):
    folder_id: str | None = None
```

在 `@app.get("/api/papers")` 前加文件夹接口：

```python
@app.get("/api/folders")
async def list_folders() -> dict:
    return {"folders": store.list_folders()}


@app.post("/api/folders", status_code=201)
async def create_folder(req: FolderIn) -> dict:
    try:
        return store.create_folder(req.name)
    except FolderExistsError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except FolderNameError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.patch("/api/folders/{folder_id}")
async def rename_folder(folder_id: str, req: FolderIn) -> dict:
    try:
        folder = store.rename_folder(folder_id, req.name)
    except FolderExistsError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except FolderNameError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if folder is None:
        raise HTTPException(status_code=404, detail="文件夹不存在")
    return folder


@app.delete("/api/folders/{folder_id}")
async def delete_folder(folder_id: str) -> dict:
    result = store.delete_folder(folder_id)
    if result is None:
        raise HTTPException(status_code=404, detail="文件夹不存在")
    return result
```

在 `delete_paper` 后加移动接口：

```python
@app.patch("/api/papers/{paper_id}/folder")
async def move_paper_to_folder(paper_id: str, req: PaperFolderIn) -> dict:
    try:
        row = store.move_paper(paper_id, req.folder_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    if row is None:
        raise HTTPException(status_code=404, detail="论文不存在")
    return _public(row)
```

把 `upload_papers` 签名和实现改成：

```python
@app.post("/api/papers/upload")
async def upload_papers(
    files: list[UploadFile] = File(...),
    folder_id: str | None = Form(default=None),
) -> dict:
    if folder_id and store.get_folder(folder_id) is None:
        raise HTTPException(status_code=404, detail="文件夹不存在")

    added, failed = [], []
    for f in files:
        name = f.filename or "paper.pdf"
        if not name.lower().endswith(".pdf"):
            failed.append({"filename": name, "error": "只支持 PDF"})
            continue
        data = await f.read()
        if len(data) < 1024:
            failed.append({"filename": name, "error": "文件过小，可能不是有效 PDF"})
            continue
        meta = {"folder_id": folder_id} if folder_id else None
        row = store.add(name, data, meta)
        added.append(_public(row))
        asyncio.create_task(_parse_task(row["id"]))
    return {"added": added, "failed": failed}
```

- [ ] **Step 4: 运行 API 测试**

Run: `pytest tests/test_library_folders.py -q`

Expected: PASS（7 passed）。

- [ ] **Step 5: 跑现有后端测试**

Run: `pytest -q`

Expected: 全量 PASS。

- [ ] **Step 6: 提交**

```bash
git add server/main.py tests/test_library_folders.py
git commit -m "feat(library): add folder API and upload target"
```

---

## Task 3: 前端文件夹基础（API / 图标 / 侧栏组件 / 样式）

**Files:**
- Modify: `web/src/lib/library.ts`
- Modify: `web/src/components/icons.tsx`
- Create: `web/src/components/LibraryFolders.tsx`
- Modify: `web/src/components/Sidebar.tsx`
- Modify: `web/src/styles.css`

- [ ] **Step 1: 补 `Folder` 类型与 API**

在 `web/src/lib/library.ts` 的 `PaperRow` 中加：

```ts
  folder_id?: string | null
```

在 `PaperRow` 后加：

```ts
export interface Folder {
  id: string
  name: string
  created_at: number
  paper_count: number
}
```

把 `listPapers` 后加：

```ts
export async function listFolders(): Promise<Folder[]> {
  const data = await jsonFetch<{ folders: Folder[] }>('/api/folders')
  return data.folders
}

export async function createFolder(name: string): Promise<Folder> {
  return jsonFetch('/api/folders', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  })
}

export async function renameFolder(id: string, name: string): Promise<Folder> {
  return jsonFetch(`/api/folders/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  })
}

export async function deleteFolder(
  id: string,
): Promise<{ ok: boolean; moved: number }> {
  return jsonFetch(`/api/folders/${id}`, { method: 'DELETE' })
}

export async function movePaperToFolder(
  paperId: string,
  folderId: string | null,
): Promise<PaperRow> {
  return jsonFetch(`/api/papers/${paperId}/folder`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ folder_id: folderId }),
  })
}
```

把 `uploadPapers` 改成：

```ts
export async function uploadPapers(
  files: File[],
  folderId: string | null = null,
): Promise<{ added: PaperRow[]; failed: { filename: string; error: string }[] }> {
  const fd = new FormData()
  files.forEach((f) => fd.append('files', f))
  if (folderId) fd.append('folder_id', folderId)
  return jsonFetch('/api/papers/upload', { method: 'POST', body: fd })
}
```

- [ ] **Step 2: 新增文件夹图标**

在 `web/src/components/icons.tsx` 的 `IconFolder` 后加：

```tsx
export function IconFolderPapers() {
  return (
    <svg
      width="15"
      height="15"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z" />
      <path d="M8 4.5h5l2 2" opacity=".55" />
      <path d="M9 10h6M9 13h4" opacity=".8" />
    </svg>
  )
}
```

- [ ] **Step 3: 新建侧栏文件夹组件**

创建 `web/src/components/LibraryFolders.tsx`：

```tsx
import { useEffect, useRef, useState, type DragEvent } from 'react'
import type { Folder } from '../lib/library'
import { IconBook, IconFolder, IconFolderPapers, IconPlus } from './icons'

interface Props {
  folders: Folder[]
  totalCount: number
  activeFolderId: string | null
  loading: boolean
  error: string | null
  dragPaperId: string | null
  dragOverFolderId: string | null
  onSelect: (folderId: string | null) => void
  onCreate: (name: string) => Promise<boolean>
  onRename: (folderId: string, name: string) => Promise<boolean>
  onDelete: (folder: Folder) => void
  onDragEnterFolder: (folderId: string) => void
  onDragLeaveFolder: (folderId: string) => void
  onDropFolder: (folderId: string) => void
}

export function LibraryFolders({
  folders,
  totalCount,
  activeFolderId,
  loading,
  error,
  dragPaperId,
  dragOverFolderId,
  onSelect,
  onCreate,
  onRename,
  onDelete,
  onDragEnterFolder,
  onDragLeaveFolder,
  onDropFolder,
}: Props) {
  const [creating, setCreating] = useState(false)
  const [draft, setDraft] = useState('')
  const [renamingId, setRenamingId] = useState<string | null>(null)
  const [renameDraft, setRenameDraft] = useState('')
  const [menuId, setMenuId] = useState<string | null>(null)

  const createRef = useRef<HTMLInputElement>(null)
  const renameRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLDivElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    createRef.current?.focus()
  }, [creating])

  useEffect(() => {
    renameRef.current?.focus()
    renameRef.current?.select()
  }, [renamingId])

  useEffect(() => {
    if (!menuId) return
    const onDown = (event: MouseEvent) => {
      if (menuRef.current?.contains(event.target as Node)) return
      setMenuId(null)
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setMenuId(null)
    }
    document.addEventListener('mousedown', onDown)
    window.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      window.removeEventListener('keydown', onKey)
    }
  }, [menuId])

  const beginCreate = () => {
    setMenuId(null)
    setCreating(true)
    setDraft('')
  }

  const submitCreate = async () => {
    const name = draft.trim()
    if (!name) {
      setCreating(false)
      return
    }
    const ok = await onCreate(name)
    if (ok) {
      setCreating(false)
      setDraft('')
    }
  }

  const beginRename = (folder: Folder) => {
    setMenuId(null)
    setRenamingId(folder.id)
    setRenameDraft(folder.name)
  }

  const submitRename = async (folder: Folder) => {
    const name = renameDraft.trim()
    if (!name || name === folder.name) {
      setRenamingId(null)
      return
    }
    await onRename(folder.id, name)
    setRenamingId(null)
  }

  const onRowDragOver = (event: DragEvent<HTMLDivElement>, folder: Folder) => {
    if (!dragPaperId) return
    event.preventDefault()
    event.stopPropagation()
    event.dataTransfer.dropEffect = 'move'
    if (dragOverFolderId !== folder.id) onDragEnterFolder(folder.id)
  }

  const onRowDragLeave = (event: DragEvent<HTMLDivElement>, folder: Folder) => {
    if (event.currentTarget.contains(event.relatedTarget as Node)) return
    onDragLeaveFolder(folder.id)
  }

  const onRowDrop = (event: DragEvent<HTMLDivElement>, folder: Folder) => {
    event.preventDefault()
    event.stopPropagation()
    onDropFolder(folder.id)
  }

  const onListDragOver = (event: DragEvent<HTMLDivElement>) => {
    if (!dragPaperId || !listRef.current) return
    const rect = listRef.current.getBoundingClientRect()
    if (event.clientY < rect.top + 34) listRef.current.scrollTop -= 9
    else if (event.clientY > rect.bottom - 34) listRef.current.scrollTop += 9
  }

  return (
    <div className="library-folders">
      <div className="folders-head">
        <span>文件夹</span>
        <button
          className="icon-btn"
          title="新建文件夹"
          onClick={beginCreate}
          disabled={creating}
        >
          <IconPlus />
        </button>
      </div>

      {creating && (
        <form
          className="folder-create"
          onSubmit={(event) => {
            event.preventDefault()
            void submitCreate()
          }}
        >
          <input
            ref={createRef}
            value={draft}
            maxLength={40}
            placeholder="文件夹名称"
            onChange={(event) => setDraft(event.target.value)}
            onBlur={() => void submitCreate()}
            onKeyDown={(event) => {
              if (event.key === 'Escape') {
                setCreating(false)
                setDraft('')
              }
            }}
          />
        </form>
      )}

      {loading ? (
        <p className="folders-empty">正在加载…</p>
      ) : error ? (
        <p className="folders-empty">{error}</p>
      ) : (
        <>
          <div
            className="folder-list"
            ref={listRef}
            onDragOver={onListDragOver}
          >
            <div
              className="folder-row folder-all"
              role="button"
              tabIndex={0}
              data-active={!activeFolderId}
              onClick={() => onSelect(null)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') onSelect(null)
              }}
            >
              <i className="folder-icon">
                <IconBook />
              </i>
              <span className="folder-name">全部文献</span>
              <em className="folder-count">{totalCount}</em>
            </div>

            {folders.map((folder) => (
              <div className="folder-row-wrap" key={folder.id}>
                <div
                  className="folder-row"
                  role="button"
                  tabIndex={0}
                  data-active={folder.id === activeFolderId}
                  data-empty={folder.paper_count === 0}
                  data-drop={folder.id === dragOverFolderId}
                  onClick={() => {
                    if (renamingId === folder.id) return
                    onSelect(folder.id)
                  }}
                  onDoubleClick={() => beginRename(folder)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' && renamingId !== folder.id) {
                      onSelect(folder.id)
                    }
                  }}
                  onDragOver={(event) => onRowDragOver(event, folder)}
                  onDragLeave={(event) => onRowDragLeave(event, folder)}
                  onDrop={(event) => onRowDrop(event, folder)}
                >
                  <i className="folder-icon">
                    {folder.paper_count > 0 ? (
                      <IconFolderPapers />
                    ) : (
                      <IconFolder />
                    )}
                  </i>

                  {renamingId === folder.id ? (
                    <input
                      ref={renameRef}
                      className="folder-rename"
                      value={renameDraft}
                      maxLength={40}
                      onClick={(event) => event.stopPropagation()}
                      onChange={(event) => setRenameDraft(event.target.value)}
                      onBlur={() => void submitRename(folder)}
                      onKeyDown={(event) => {
                        if (event.key === 'Enter') {
                          event.preventDefault()
                          event.currentTarget.blur()
                        }
                        if (event.key === 'Escape') {
                          event.preventDefault()
                          setRenamingId(null)
                        }
                      }}
                    />
                  ) : (
                    <span className="folder-name">{folder.name}</span>
                  )}

                  {renamingId !== folder.id && folder.paper_count > 0 && (
                    <em className="folder-count">{folder.paper_count}</em>
                  )}

                  {renamingId !== folder.id && dragPaperId && (
                    <span className="folder-drop-label">松开移入</span>
                  )}
                </div>

                {renamingId !== folder.id && (
                  <button
                    className="folder-more"
                    title="文件夹操作"
                    onClick={(event) => {
                      event.stopPropagation()
                      setMenuId((prev) => (prev === folder.id ? null : folder.id))
                    }}
                  >
                    ···
                  </button>
                )}

                {menuId === folder.id && (
                  <div className="folder-menu" ref={menuRef} role="menu">
                    <button
                      role="menuitem"
                      onClick={() => beginRename(folder)}
                    >
                      重命名
                    </button>
                    <button
                      role="menuitem"
                      className="is-danger"
                      onClick={() => {
                        setMenuId(null)
                        onDelete(folder)
                      }}
                    >
                      删除文件夹
                    </button>
                  </div>
                )}
              </div>
            ))}
          </div>

          {folders.length === 0 && !creating && (
            <div className="folders-empty">
              <p>还没有文件夹</p>
              <button className="folder-empty-create" onClick={beginCreate}>
                <IconPlus />
                新建文件夹
              </button>
            </div>
          )}
        </>
      )}
    </div>
  )
}
```

- [ ] **Step 4: Sidebar 支持文件夹插槽**

修改 `web/src/components/Sidebar.tsx`：

```tsx
interface Props {
  active: NavKey
  count?: number
  onNav: (key: NavKey) => void
  onSettings: () => void
  /** 研究/写综述时用它替换“文件夹”分区 */
  projectSection?: ReactNode
  /** 文献库用它渲染文件夹分区 */
  folderSection?: ReactNode
}

export function Sidebar({
  active,
  count,
  onNav,
  onSettings,
  projectSection,
  folderSection,
}: Props) {
```

把：

```tsx
      {projectSection ?? (
        <div className="library-folders">
```

改成：

```tsx
      {projectSection ?? folderSection ?? (
        <div className="library-folders">
```

- [ ] **Step 5: 加 CSS**

在 `web/src/styles.css` 的 `.library` 规则里加变量：

```css
  --library-side-w: clamp(250px, 18vw, 290px);
  grid-template-columns: var(--library-side-w) minmax(0, 1fr);
```

把原来的 `grid-template-columns` 删掉，替换为上面两行。

在 `.folders-empty` 后追加：

```css
.folder-list {
  display: flex;
  flex-direction: column;
  gap: 1px;
  overflow-y: auto;
  min-height: 0;
  max-height: calc(100vh - 300px);
}

.folder-row-wrap {
  position: relative;
}

.folder-row {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  height: 32px;
  padding: 0 7px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--ink-2);
  font-size: 13px;
  text-align: left;
  cursor: pointer;
  user-select: none;
  transition: background 0.16s ease, box-shadow 0.16s ease, color 0.16s ease;
}

.folder-row:hover {
  background: var(--bg-soft);
}

.folder-row[data-active='true'] {
  background: var(--blue-soft);
  color: var(--blue-ink);
}

.folder-row[data-drop='true'] {
  background: var(--blue-soft);
  box-shadow: inset 0 0 0 1px var(--blue-line);
}

.folder-icon {
  flex: none;
  display: grid;
  place-items: center;
  color: var(--faint);
  transition: color 0.16s ease, transform 0.16s ease;
}

.folder-row[data-active='true'] .folder-icon {
  color: var(--blue);
}

.folder-row[data-drop='true'] .folder-icon {
  color: var(--blue);
  transform: translateY(-1px) scale(1.05);
}

.folder-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.folder-count {
  margin-left: auto;
  min-width: 18px;
  padding: 1px 6px;
  border-radius: 99px;
  background: var(--bg-soft);
  color: var(--muted);
  font-size: 10.5px;
  font-style: normal;
  text-align: center;
  flex: none;
}

.folder-row[data-active='true'] .folder-count {
  background: #fff;
  color: var(--blue-ink);
}

.folder-drop-label {
  margin-left: 6px;
  color: var(--blue-ink);
  font-size: 10.5px;
  white-space: nowrap;
}

.folder-more {
  position: absolute;
  right: 2px;
  top: 5px;
  width: 22px;
  height: 22px;
  border-radius: 6px;
  color: var(--faint);
  opacity: 0;
  transition: opacity 0.15s ease, background 0.15s ease;
}

.folder-row-wrap:hover .folder-more,
.folder-more:focus-visible {
  opacity: 1;
}

.folder-more:hover {
  background: var(--bg-soft);
  color: var(--ink-2);
}

.folder-menu {
  position: absolute;
  right: 2px;
  top: 31px;
  z-index: 30;
  min-width: 132px;
  padding: 4px;
  border: 1px solid var(--line);
  border-radius: 9px;
  background: #fff;
  box-shadow: var(--shadow-2);
}

.folder-menu button {
  display: block;
  width: 100%;
  padding: 7px 9px;
  border-radius: 6px;
  color: var(--ink-2);
  font-size: 12px;
  text-align: left;
}

.folder-menu button:hover {
  background: var(--bg-soft);
}

.folder-menu button.is-danger {
  color: #b91c1c;
}

.folder-create {
  padding: 0 2px 4px;
}

.folder-create input,
.folder-rename {
  width: 100%;
  height: 28px;
  padding: 0 7px;
  border: 1px solid var(--blue-line);
  border-radius: 7px;
  background: #fff;
  color: var(--ink);
  font-size: 12.5px;
  outline: none;
}

.folder-empty-create {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin-top: 4px;
  padding: 4px 8px;
  border: 1px solid var(--line-2);
  border-radius: 7px;
  background: var(--bg);
  color: var(--ink-2);
  font-size: 12px;
}

.folder-empty-create:hover {
  border-color: var(--blue-line);
  color: var(--blue-ink);
}
```

- [ ] **Step 6: 构建**

Run: `cd web && npm run build`

Expected: TypeScript + Vite 构建通过。

- [ ] **Step 7: 提交**

```bash
git add web/src/lib/library.ts web/src/components/icons.tsx \
  web/src/components/LibraryFolders.tsx web/src/components/Sidebar.tsx \
  web/src/styles.css
git commit -m "feat(library): add folder sidebar foundation"
```

---

## Task 4: LibraryPage 接入文件夹 CRUD / 过滤 / 上传归属

**Files:**
- Modify: `web/src/components/LibraryPage.tsx`

- [ ] **Step 1: 改导入与状态**

把 `LibraryPage.tsx` 的 library 导入改成：

```tsx
import {
  DEMO_PAPER,
  createFolder,
  deleteFolder,
  listFolders,
  listPapers,
  renameFolder,
  reparsePaper,
  uploadPapers,
  type Folder,
  type PaperRow,
} from '../lib/library'
```

加：

```tsx
import { IconBack, IconFolder } from './icons'
import { LibraryFolders } from './LibraryFolders'
```

在 `LibraryPage` 内、现有 state 后加：

```tsx
  const [folders, setFolders] = useState<Folder[]>([])
  const [foldersLoading, setFoldersLoading] = useState(true)
  const [folderError, setFolderError] = useState<string | null>(null)
  const [activeFolderId, setActiveFolderId] = useState<string | null>(null)
  const [gridAnimating, setGridAnimating] = useState(false)
```

在 `toast` state 后加辅助函数：

```tsx
  const errorText = (error: unknown) =>
    error instanceof Error ? error.message : '操作失败，请重试'
```

- [ ] **Step 2: refresh 同时加载文件夹**

把现有 `refresh` 改成：

```tsx
  const refresh = useCallback(async () => {
    try {
      const rows = await listPapers()
      setPapers(rows)
      setDemoMode(false)
      try {
        setFolders(await listFolders())
        setFolderError(null)
      } catch (error) {
        setFolderError(errorText(error))
      }
    } catch {
      // 后端没起：退回内置示例，页面依然可看
      setPapers([DEMO_PAPER])
      setDemoMode(true)
      setFolders([])
      setFolderError(null)
    } finally {
      setLoading(false)
      setFoldersLoading(false)
    }
  }, [])
```

- [ ] **Step 3: 加派生数据与文件夹 CRUD**

在 `visible` useMemo 前加：

```tsx
  const folderCounts = useMemo(() => {
    const counts = new Map<string, number>()
    for (const paper of papers) {
      if (paper.folder_id) {
        counts.set(paper.folder_id, (counts.get(paper.folder_id) ?? 0) + 1)
      }
    }
    return counts
  }, [papers])

  const folderRows = useMemo(
    () =>
      folders.map((folder) => ({
        ...folder,
        paper_count: folderCounts.get(folder.id) ?? 0,
      })),
    [folders, folderCounts],
  )

  const selectedFolder =
    folders.find((folder) => folder.id === activeFolderId) ?? null

  const folderPapers = useMemo(
    () =>
      activeFolderId
        ? papers.filter((paper) => paper.folder_id === activeFolderId)
        : papers,
    [papers, activeFolderId],
  )
```

在 `onReparse` 后加：

```tsx
  useEffect(() => {
    if (activeFolderId && !folders.some((folder) => folder.id === activeFolderId)) {
      setActiveFolderId(null)
    }
  }, [folders, activeFolderId])

  useEffect(() => {
    setGridAnimating(true)
    const timer = window.setTimeout(() => setGridAnimating(false), 180)
    return () => window.clearTimeout(timer)
  }, [activeFolderId])

  const handleCreateFolder = async (name: string) => {
    try {
      const folder = await createFolder(name)
      setFolders((prev) => [...prev, folder])
      setActiveFolderId(folder.id)
      notify(`已创建「${folder.name}」`)
      return true
    } catch (error) {
      notify(errorText(error), 'err')
      return false
    }
  }

  const handleRenameFolder = async (folderId: string, name: string) => {
    try {
      const folder = await renameFolder(folderId, name)
      setFolders((prev) =>
        prev.map((item) => (item.id === folder.id ? folder : item)),
      )
      notify('文件夹已重命名')
      return true
    } catch (error) {
      notify(errorText(error), 'err')
      return false
    }
  }

  const handleDeleteFolder = async (folder: Folder) => {
    const count = folderCounts.get(folder.id) ?? 0
    if (
      !window.confirm(
        `删除文件夹「${folder.name}」？里面的 ${count} 篇论文会回到全部文献，不会删除论文。`,
      )
    ) {
      return
    }
    const previousFolders = folders
    const previousPapers = papers
    const previousActive = activeFolderId
    setFolders((prev) => prev.filter((item) => item.id !== folder.id))
    setPapers((prev) =>
      prev.map((paper) =>
        paper.folder_id === folder.id ? { ...paper, folder_id: null } : paper,
      ),
    )
    if (activeFolderId === folder.id) setActiveFolderId(null)
    try {
      const result = await deleteFolder(folder.id)
      notify(
        `已删除「${folder.name}」${
          result.moved ? `，${result.moved} 篇论文回到全部文献` : ''
        }`,
      )
    } catch (error) {
      setFolders(previousFolders)
      setPapers(previousPapers)
      setActiveFolderId(previousActive)
      notify(errorText(error), 'err')
    }
  }

- [ ] **Step 4: 过滤当前文件夹 + 上传归属**

把 `visible` 的 filter 开头改成：

```tsx
    let rows = papers.filter((p) => {
      if (activeFolderId && p.folder_id !== activeFolderId) return false
      const q = query.trim().toLowerCase()
      if (q) {
        const hay = `${p.title} ${p.authors} ${p.filename}`.toLowerCase()
        if (!hay.includes(q)) return false
      }
      if (filter === 'ready') return p.status === 'ready'
      if (filter === 'untranslated') return p.translated === 0 && p.status === 'ready'
      if (filter === 'failed') return p.status === 'failed'
      return true
    })
```

把 `useMemo` 依赖改成：

```tsx
  }, [papers, query, filter, sort, activeFolderId])
```

把 `doUpload` 里的：

```tsx
      const res = await uploadPapers(pdfs)
```

改成：

```tsx
      const res = await uploadPapers(pdfs, activeFolderId)
```

把 Zotero 导入的 toast 改成：

```tsx
            notify(
              `已从 Zotero 导入 ${imported} 篇${
                skipped ? `，跳过 ${skipped} 篇` : ''
              }${activeFolderId ? '到全部文献（未自动归档）' : ''}，正在解析…`,
            )
```

- [ ] **Step 5: 侧栏与标题**

把 `<Sidebar ... />` 改成：

```tsx
      <Sidebar
        active="library"
        count={papers.length}
        onNav={onNav}
        onSettings={() => setSettingsOpen(true)}
        folderSection={
          <LibraryFolders
            folders={folderRows}
            totalCount={papers.length}
            activeFolderId={activeFolderId}
            loading={foldersLoading}
            error={folderError}
            dragPaperId={null}
            dragOverFolderId={null}
            onSelect={setActiveFolderId}
            onCreate={handleCreateFolder}
            onRename={handleRenameFolder}
            onDelete={(folder) => void handleDeleteFolder(folder)}
            onDragEnterFolder={() => undefined}
            onDragLeaveFolder={() => undefined}
            onDropFolder={() => undefined}
          />
        }
      />
```

把 `<h1>文献库</h1>` 这一段改成：

```tsx
        <div className="library-title">
          {selectedFolder && (
            <button
              className="library-crumb"
              onClick={() => setActiveFolderId(null)}
              title="返回全部文献"
            >
              <IconBack />
              全部文献
            </button>
          )}
          <h1>{selectedFolder?.name ?? '文献库'}</h1>
          {selectedFolder && (
            <span className="library-count">{folderPapers.length} 篇</span>
          )}
        </div>
```

- [ ] **Step 6: 空状态与网格 class**

把空状态里的：

```tsx
            {papers.length === 0 ? (
              <>
                <div className="dropzone" onClick={() => inputRef.current?.click()}>
```

改成：

```tsx
            {papers.length === 0 ? (
              <>
                <div className="dropzone" onClick={() => inputRef.current?.click()}>
```

保持原样；在 `papers.length === 0` 分支后加：

```tsx
            ) : selectedFolder && folderPapers.length === 0 ? (
              <div className="folder-empty">
                <IconFolder />
                <b>这个文件夹还是空的</b>
                <span>把论文拖到左侧文件夹里</span>
                <button className="btn primary" onClick={() => inputRef.current?.click()}>
                  添加文章
                </button>
              </div>
            ) : (
              <span>没有符合条件的文章</span>
            )}
```

把原来的 `) : ( <span>没有符合条件的文章</span> )` 删掉，确保三元闭合。

把网格：

```tsx
          <div className={`paper-grid ${view === 'list' ? 'is-list' : ''}`}>
```

改成：

```tsx
          <div
            className={`paper-grid ${view === 'list' ? 'is-list' : ''} ${
              gridAnimating ? 'is-switching' : ''
            }`}
          >
```

- [ ] **Step 7: 加标题与空状态 CSS**

在 `styles.css` 的 `.library-head h1` 后加：

```css
.library-title {
  display: flex;
  align-items: center;
  gap: 9px;
  min-width: 0;
}

.library-crumb {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 3px 7px;
  border: 1px solid var(--line);
  border-radius: 7px;
  background: #fff;
  color: var(--muted);
  font-size: 11.5px;
}

.library-crumb:hover {
  border-color: var(--blue-line);
  color: var(--blue-ink);
}

.library-count {
  color: var(--faint);
  font-size: 12px;
}

.folder-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 54px 20px;
}

.folder-empty > svg {
  width: 34px;
  height: 34px;
  color: var(--faint);
}

.folder-empty b {
  color: var(--ink);
  font-size: 14px;
}

.folder-empty span {
  color: var(--faint);
  font-size: 12.5px;
}
```

- [ ] **Step 8: 构建并手工验证**

Run: `cd web && npm run build`

Expected: 构建通过。

启动：`python -m server.main`，在浏览器验证：

- 点 `+` 新建文件夹，空文件夹线框图标、无角标。
- 点文件夹切换，标题、篇数、返回全部正确。
- 双击文件夹重命名；`…` 菜单删除；删除后论文回到全部。
- 在文件夹内上传 PDF，论文进当前文件夹。

- [ ] **Step 9: 提交**

```bash
git add web/src/components/LibraryPage.tsx web/src/styles.css
git commit -m "feat(library): wire folder selection and CRUD"
```

---

## Task 5: 论文拖拽、文件夹投放与 0.7 秒左边缘移出

**Files:**
- Modify: `web/src/components/PaperCard.tsx`
- Modify: `web/src/components/LibraryPage.tsx`
- Modify: `web/src/styles.css`

- [ ] **Step 1: PaperCard 支持拖拽**

把 `PaperCard` 的 Props 改成：

```tsx
interface Props {
  paper: PaperRow
  view: 'grid' | 'list'
  onOpen: (id: string) => void
  onDelete: (paper: PaperRow) => void
  onReparse: (paper: PaperRow) => void
  draggable?: boolean
  dragging?: boolean
  onDragStart?: (paper: PaperRow) => void
  onDragEnd?: () => void
}
```

函数签名加参数：

```tsx
export function PaperCard({
  paper,
  view,
  onOpen,
  onDelete,
  onReparse,
  draggable = false,
  dragging = false,
  onDragStart,
  onDragEnd,
}: Props) {
```

把根 `<article>` 改成：

```tsx
    <article
      className={`paper-card ${view === 'list' ? 'is-list' : ''}`}
      onClick={() => onOpen(paper.id)}
      title={paper.title}
      draggable={draggable}
      data-dragging={dragging}
      onDragStart={(event) => {
        if (!draggable) return
        event.dataTransfer.effectAllowed = 'move'
        event.dataTransfer.setData('text/plain', paper.id)
        onDragStart?.(paper)
      }}
      onDragEnd={() => onDragEnd?.()}
    >
```

- [ ] **Step 2: LibraryPage 增加拖拽状态与处理**

在 `LibraryPage` 的 state 后加：

```tsx
  const [dragPaper, setDragPaper] = useState<PaperRow | null>(null)
  const [dragOverFolderId, setDragOverFolderId] = useState<string | null>(null)
  const [dwellActive, setDwellActive] = useState(false)
  const [dragOutDone, setDragOutDone] = useState(false)
  const [dragFolderName, setDragFolderName] = useState('')
  const dwellTimer = useRef<number | undefined>(undefined)
  const movingOut = useRef(false)
  const postDragFolder = useRef<string | null | undefined>(undefined)
  const suppressClick = useRef(false)
```

把 `visible` 的 filter 开头改成（拖拽中的卡片即使 `folder_id` 已经变化也保留在当前网格）：

```tsx
    let rows = papers.filter((p) => {
      if (
        activeFolderId &&
        p.folder_id !== activeFolderId &&
        p.id !== dragPaper?.id
      ) {
        return false
      }
```

并把 `visible` 的依赖数组补上 `dragPaper?.id`：

```tsx
  }, [papers, query, filter, sort, activeFolderId, dragPaper?.id])
```

先把 `movePaperToFolder` 加回 `web/src/lib/library` 的导入，然后在 `handleDeleteFolder` 后加：

```tsx
  const movePaper = useCallback(
    async (paperId: string, folderId: string | null) => {
      const paper = papers.find((item) => item.id === paperId)
      if (!paper) return false
      const previous = paper.folder_id ?? null
      if (previous === folderId) return true
      setPapers((prev) =>
        prev.map((item) =>
          item.id === paperId ? { ...item, folder_id: folderId } : item,
        ),
      )
      try {
        await movePaperToFolder(paperId, folderId)
        notify(
          folderId
            ? `已移入「${folders.find((f) => f.id === folderId)?.name ?? '文件夹'}」`
            : '已移出到全部文献',
        )
        return true
      } catch (error) {
        setPapers((prev) =>
          prev.map((item) =>
            item.id === paperId ? { ...item, folder_id: previous } : item,
          ),
        )
        notify(errorText(error), 'err')
        return false
      }
    },
    [papers, folders, notify],
  )
```

然后加：

```tsx
  const cancelDwell = useCallback(() => {
    if (dwellTimer.current !== undefined) {
      window.clearTimeout(dwellTimer.current)
      dwellTimer.current = undefined
    }
    setDwellActive(false)
  }, [])

  const completeDragOut = useCallback(async () => {
    if (!dragPaper?.folder_id || movingOut.current) return
    movingOut.current = true
    const paper = dragPaper
    const moved = await movePaper(paper.id, null)
    movingOut.current = false
    if (!moved) return
    setDragPaper((prev) => (prev ? { ...prev, folder_id: null } : prev))
    if (activeFolderId) postDragFolder.current = null
    setDragOutDone(true)
    window.setTimeout(() => setDragOutDone(false), 350)
  }, [dragPaper, activeFolderId, movePaper])

  const startDwell = useCallback(() => {
    if (
      !dragPaper?.folder_id ||
      dwellTimer.current !== undefined ||
      dragOutDone ||
      movingOut.current
    ) {
      return
    }
    setDwellActive(true)
    dwellTimer.current = window.setTimeout(() => {
      dwellTimer.current = undefined
      void completeDragOut()
    }, 700)
  }, [dragPaper, dragOutDone, completeDragOut])

  const handlePaperDragStart = (paper: PaperRow) => {
    suppressClick.current = true
    setDragPaper(paper)
    setDragOverFolderId(null)
    setDragOutDone(false)
    postDragFolder.current = undefined
    setDragFolderName(
      folders.find((folder) => folder.id === paper.folder_id)?.name ?? '',
    )
  }

  const handlePaperDragEnd = () => {
    cancelDwell()
    setDragPaper(null)
    setDragOverFolderId(null)
    setDragOutDone(false)
    const target = postDragFolder.current
    postDragFolder.current = undefined
    if (target !== undefined) setActiveFolderId(target)
    window.setTimeout(() => {
      suppressClick.current = false
    }, 180)
  }

  const handleDropOnFolder = (folderId: string) => {
    const paper = dragPaper
    setDragOverFolderId(null)
    if (!paper) return
    if ((paper.folder_id ?? null) === folderId) return
    if (activeFolderId && activeFolderId !== folderId) {
      postDragFolder.current = folderId
    }
    void movePaper(paper.id, folderId)
  }

  const openPaper = (id: string) => {
    if (suppressClick.current) return
    onOpenPaper(id)
  }
```

- [ ] **Step 3: 修正外部文件拖入与内部卡片拖拽冲突**

把根 `.library` 的 `onDragOver` / `onDragLeave` / `onDrop` 改成：

```tsx
      onDragOver={(e) => {
        if (!Array.from(e.dataTransfer.types).includes('Files')) return
        e.preventDefault()
        setDragOver(true)
      }}
      onDragLeave={() => {
        if (dragOver) setDragOver(false)
      }}
      onDrop={(e) => {
        if (e.dataTransfer.files.length === 0) return
        e.preventDefault()
        setDragOver(false)
        void doUpload(Array.from(e.dataTransfer.files))
      }}
```

- [ ] **Step 4: 传入拖拽状态与文件夹回调**

把 `<LibraryFolders>` 改成：

```tsx
          <LibraryFolders
            folders={folderRows}
            totalCount={papers.length}
            activeFolderId={activeFolderId}
            loading={foldersLoading}
            error={folderError}
            dragPaperId={dragPaper?.id ?? null}
            dragOverFolderId={dragOverFolderId}
            onSelect={setActiveFolderId}
            onCreate={handleCreateFolder}
            onRename={handleRenameFolder}
            onDelete={(folder) => void handleDeleteFolder(folder)}
            onDragEnterFolder={setDragOverFolderId}
            onDragLeaveFolder={(folderId) =>
              setDragOverFolderId((prev) => (prev === folderId ? null : prev))
            }
            onDropFolder={handleDropOnFolder}
          />
```

把 `PaperCard` 调用改成：

```tsx
            <PaperCard
              key={p.id}
              paper={p}
              view={view}
              onOpen={openPaper}
              onDelete={onDelete}
              onReparse={onReparse}
              draggable={!demoMode}
              dragging={dragPaper?.id === p.id}
              onDragStart={handlePaperDragStart}
              onDragEnd={handlePaperDragEnd}
            />
```

- [ ] **Step 5: 左边缘 0.7 秒投放条**

在 `LibraryPage` 返回值里的 `.library` 结束标签前（`</div>` 前）加：

```tsx
      {dragPaper && (dragPaper.folder_id || dragOutDone) && (
        <div
          className="folder-out-rail"
          data-open={true}
          data-done={dragOutDone}
          data-dwelling={dwellActive}
          onDragEnter={(event) => {
            event.preventDefault()
            startDwell()
          }}
          onDragOver={(event) => {
            event.preventDefault()
            event.dataTransfer.dropEffect = 'move'
            startDwell()
          }}
          onDragLeave={(event) => {
            if (event.currentTarget.contains(event.relatedTarget as Node)) return
            cancelDwell()
          }}
          onDrop={(event) => {
            event.preventDefault()
            cancelDwell()
          }}
        >
          <span className="folder-out-icon">
            <IconFolder />
          </span>
          <b>{dragOutDone ? '已移出' : '全部文献'}</b>
          <span className="folder-out-sub">
            {dragOutDone ? '论文已回到全部文献' : `从「${dragFolderName}」移出`}
          </span>
          {!dragOutDone && <small>停留 0.7 秒移出</small>}
          <span className="folder-out-progress">
            <i style={{ width: dwellActive ? '100%' : '0%' }} />
          </span>
        </div>
      )}
```

- [ ] **Step 6: 加拖拽/投放条 CSS**

在 `styles.css` 的 `.drop-overlay` 前加：

```css
.paper-card[data-dragging='true'] {
  opacity: 0.55;
  transform: scale(0.985);
}

.folder-out-rail {
  position: fixed;
  left: var(--library-side-w);
  top: 50%;
  z-index: 80;
  display: flex;
  flex-direction: column;
  gap: 4px;
  width: 188px;
  padding: 14px;
  border: 1px solid var(--blue-line);
  border-radius: 12px;
  background: #fff;
  box-shadow: 0 14px 36px rgba(37, 99, 235, 0.16);
  transform: translate(-110%, -50%);
  opacity: 0;
  pointer-events: none;
  transition: transform 0.18s ease, opacity 0.18s ease;
}

.folder-out-rail[data-open='true'] {
  animation: folder-rail-in 0.18s ease both;
  transform: translate(10px, -50%);
  opacity: 1;
  pointer-events: auto;
}

@keyframes folder-rail-in {
  from {
    transform: translate(-110%, -50%);
    opacity: 0;
  }
  to {
    transform: translate(10px, -50%);
    opacity: 1;
  }
}

.folder-out-icon {
  display: grid;
  place-items: center;
  width: 30px;
  height: 30px;
  border-radius: 8px;
  background: var(--blue-soft);
  color: var(--blue);
}

.folder-out-rail b {
  color: var(--ink);
  font-size: 13px;
}

.folder-out-sub {
  color: var(--muted);
  font-size: 11.5px;
}

.folder-out-rail small {
  color: var(--faint);
  font-size: 10.5px;
}

.folder-out-progress {
  display: block;
  height: 3px;
  margin-top: 4px;
  overflow: hidden;
  border-radius: 99px;
  background: var(--bg-soft);
}

.folder-out-progress i {
  display: block;
  height: 100%;
  background: var(--blue);
  transition: width 0.7s linear;
}

.folder-out-rail[data-done='true'] {
  border-color: #bbf7d0;
  box-shadow: 0 14px 36px rgba(22, 163, 74, 0.14);
}

.folder-out-rail[data-done='true'] .folder-out-icon {
  background: #f0fdf4;
  color: #15803d;
}

.paper-grid.is-switching {
  animation: folder-grid-in 0.16s ease both;
}

@keyframes folder-grid-in {
  from {
    opacity: 0;
    transform: translateY(4px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

@media (prefers-reduced-motion: reduce) {
  .paper-card,
  .folder-row,
  .folder-icon,
  .folder-out-rail,
  .folder-out-progress i,
  .paper-grid.is-switching {
    animation: none !important;
    transition: none !important;
  }
}

@media (max-width: 900px) {
  .folder-out-rail {
    display: none;
  }
}
```

- [ ] **Step 7: 构建**

Run: `cd web && npm run build`

Expected: 构建通过。

- [ ] **Step 8: 手工验收拖拽**

启动 `python -m server.main`，在 1500px 宽窗口验证：

- 拖论文到文件夹行：文件夹高亮、松手后数量 +1、图标切纸页。
- 文件夹 A → B：自动切到 B。
- 在文件夹内拖到主区左边缘：投放条滑出；悬停 0.7 秒进度走满、论文移出、切全部文献；中途移开不触发。
- 0.7 秒移出后继续拖到另一个文件夹：可移入，最终切到目标文件夹。
- 拖拽期间外部 PDF 拖入的全屏遮罩不出现。
- 刷新页面，文件夹归属和数量仍在。

- [ ] **Step 9: 提交**

```bash
git add web/src/components/PaperCard.tsx web/src/components/LibraryPage.tsx \
  web/src/styles.css
git commit -m "feat(library): drag papers between folders"
```

---

## Task 6: 文档、全量验证与回归

**Files:**
- Modify: `README.md`
- Verify: `pytest`, `cd web && npm run build`

- [ ] **Step 1: README 补一条能力**

在「文献库」小节的列表里加：

```md
- **文件夹管理**：新建单层文件夹，把论文拖进文件夹；空文件夹和已有论文的文件夹使用不同图标，拖到左边缘「全部文献」投放条停留 0.7 秒即可移出。
```

- [ ] **Step 2: 全量后端测试**

Run: `pytest -q`

Expected: 全量 PASS。

- [ ] **Step 3: 前端构建**

Run: `cd web && npm run build`

Expected: TypeScript 无错误，Vite 构建成功。

- [ ] **Step 4: 手工回归**

启动 `python -m server.main`，验证：

- 文献库搜索、筛选、排序仍正常，且限定在当前文件夹。
- 阅读页可正常打开。
- Zotero 导入仍进全部文献，toast 说明去向。
- 删除文件夹不删除论文。
- 空文件夹有专用空状态。
- `prefers-reduced-motion` 下无动画但不影响功能。

- [ ] **Step 5: 提交**

```bash
git add README.md
git commit -m "docs: document library folders"
```

---

## 自检

- 规格覆盖：数据模型（Task 1）、接口（Task 2）、侧栏/图标/菜单（Task 3）、CRUD/过滤/上传（Task 4）、拖拽/0.7 秒（Task 5）、文档与回归（Task 6）。
- 类型一致：`Folder`、`folder_id`、`paper_count`、`movePaper`、`DragEvent` 在前后端命名一致。
- 无占位符；每个步骤都有可执行命令或代码。
- 未引入第三方拖拽库；未改研究项目文件树。
