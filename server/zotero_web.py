"""Zotero 双向同步（Web API）。

本模块只负责与 Zotero 官方 Web API 通讯和做差异合并，不直接改前端状态。
本地没有编辑文献元数据的入口，所以合并策略是：

- 远端有、本地没有 → 下载 PDF 后加入本地文献库；
- 远端版本变化 → 用远端元数据更新本地（本地只读元数据，避免无谓冲突）；
- 本地新建的论文 / 文件夹 / 文件夹归属 → 推送到 Zotero；
- 删除是保守的：远端删除只在报告里提示，不自动删本地，避免误删阅读数据。

需要 Zotero 账号开启同步，并在 zotero.org 生成带写权限的 API Key。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

import httpx

CONFIG_NAME = "zotero-web.json"
DEFAULT_BASE = "https://api.zotero.org"

REGULAR_TYPES = {
    "artwork",
    "audioRecording",
    "bill",
    "blogPost",
    "book",
    "bookSection",
    "case",
    "conferencePaper",
    "dataset",
    "dictionaryEntry",
    "document",
    "email",
    "encyclopediaArticle",
    "film",
    "forumPost",
    "hearing",
    "instantMessage",
    "interview",
    "journalArticle",
    "letter",
    "magazineArticle",
    "manuscript",
    "map",
    "newspaperArticle",
    "patent",
    "podcast",
    "presentation",
    "radioBroadcast",
    "report",
    "standard",
    "statute",
    "thesis",
    "tvBroadcast",
    "videoRecording",
    "webpage",
}


class ZoteroWebError(Exception):
    """调用 Zotero Web API 失败。"""


def config_path(data_dir: Path) -> Path:
    return data_dir / CONFIG_NAME


def load_config(data_dir) -> dict[str, Any]:
    path = config_path(data_dir)
    defaults = {
        "api_key": "",
        "user_id": "",
        "library_type": "user",
        "group_id": "",
        "last_sync": 0,
        "last_report": None,
    }
    if not path.exists():
        return defaults
    try:
        import json

        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return defaults
    if not isinstance(payload, dict):
        return defaults
    return {**defaults, **payload}


def save_config(data_dir, config: dict[str, Any]) -> None:
    import json

    path = config_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(config, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


def public_config(data_dir) -> dict[str, Any]:
    config = load_config(data_dir)
    key = str(config.get("api_key") or "")
    return {
        "configured": bool(key and config.get("user_id")),
        "api_key_masked": f"****{key[-4:]}" if key else "",
        "user_id": config.get("user_id") or "",
        "library_type": config.get("library_type") or "user",
        "group_id": config.get("group_id") or "",
        "last_sync": config.get("last_sync") or 0,
        "last_report": config.get("last_report"),
    }


def _split_authors(authors: str) -> list[str]:
    parts = re.split(r"[;；,，]|\band\b", authors or "")
    return [part.strip() for part in parts if part and part.strip()]


def _creators(authors: str) -> list[dict[str, str]]:
    creators: list[dict[str, str]] = []
    for name in _split_authors(authors):
        if " " in name:
            first, last = name.rsplit(" ", 1)
            creators.append(
                {
                    "creatorType": "author",
                    "firstName": first,
                    "lastName": last,
                }
            )
        else:
            creators.append({"creatorType": "author", "name": name})
    return creators


def _authors_from_item(data: dict[str, Any]) -> str:
    names: list[str] = []
    for creator in data.get("creators") or []:
        if not isinstance(creator, dict):
            continue
        if creator.get("name"):
            names.append(str(creator["name"]))
        else:
            name = " ".join(
                part
                for part in (creator.get("firstName"), creator.get("lastName"))
                if part
            ).strip()
            if name:
                names.append(name)
    return ", ".join(names)


def _meta_from_item(data: dict[str, Any]) -> dict[str, str]:
    date = str(data.get("date") or "")
    year = re.search(r"(?:19|20)\d{2}", date)
    venue = (
        data.get("publicationTitle")
        or data.get("proceedingsTitle")
        or data.get("bookTitle")
        or data.get("websiteTitle")
        or data.get("university")
        or ""
    )
    return {
        "title": str(data.get("title") or "未命名文献"),
        "authors": _authors_from_item(data),
        "year": year.group(0) if year else "",
        "venue": str(venue),
        "doi": str(data.get("DOI") or ""),
        "url": str(data.get("url") or ""),
        "abstract": str(data.get("abstractNote") or ""),
    }


def _item_payload(row: dict[str, Any], collections: list[str]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "itemType": "journalArticle",
        "title": row.get("title") or "未命名文献",
        "creators": _creators(str(row.get("authors") or "")),
        "date": str(row.get("year") or ""),
        "publicationTitle": str(row.get("venue") or ""),
        "DOI": str(row.get("doi") or ""),
        "url": str(row.get("url") or ""),
        "abstractNote": str(row.get("abstract") or ""),
        "tags": [],
        "collections": collections,
        "relations": {},
    }
    return payload


def _write_results(payload: Any) -> list[tuple[str, int]]:
    """兼容不同版本的写入返回：successful / success。"""
    if not isinstance(payload, dict):
        raise ZoteroWebError("Zotero 返回了无法识别的写入结果")
    failed = payload.get("failed") or {}
    if failed:
        detail = next(iter(failed.values()))
        raise ZoteroWebError(f"Zotero 写入失败：{detail}")
    results: list[tuple[str, int]] = []
    successful = payload.get("successful")
    if isinstance(successful, dict):
        for value in successful.values():
            if isinstance(value, dict):
                key = str(value.get("key") or "")
                version = int(value.get("version") or 0)
                if key:
                    results.append((key, version))
            elif isinstance(value, str) and value:
                results.append((value, 0))
        return results
    success = payload.get("success")
    if isinstance(success, dict):
        for value in success.values():
            if isinstance(value, str) and value:
                results.append((value, 0))
    return results


def _pick_pdf(children: list[dict[str, Any]]) -> dict[str, Any] | None:
    for child in children:
        data = child.get("data") or {}
        if data.get("itemType") != "attachment":
            continue
        content_type = str(data.get("contentType") or "").lower()
        filename = str(data.get("filename") or data.get("path") or "")
        if content_type != "application/pdf" and not filename.lower().endswith(".pdf"):
            continue
        # 只有真正存了文件的附件才有 md5；只有条目、没传上去的空附件要跳过，
        # 否则会被误认为“远端已有 PDF”，导致本地再也不会补传。
        if not data.get("md5"):
            continue
        return child
    return None


class ZoteroWebClient:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        base = os.environ.get("PAPEREAD_ZOTERO_WEB_BASE") or DEFAULT_BASE
        self.base = base.rstrip("/")
        library_type = str(config.get("library_type") or "user")
        if library_type == "group":
            self.prefix = f"/groups/{config.get('group_id')}"
        else:
            self.prefix = f"/users/{config.get('user_id')}"
        self.headers = {
            "Zotero-API-Version": "3",
            "Zotero-API-Key": str(config.get("api_key") or ""),
        }
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(90.0, connect=20.0),
            follow_redirects=False,
        )

    async def __aenter__(self) -> "ZoteroWebClient":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self._client.aclose()

    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = {**self.headers, **(kwargs.pop("headers", None) or {})}
        response = await self._client.request(
            method,
            f"{self.base}{path}",
            headers=headers,
            **kwargs,
        )
        # 让出一点时间，避免集中同步触发 Zotero 的限流。
        await asyncio.sleep(0.08)
        if response.status_code >= 400:
            text = response.text[:300].replace("\n", " ")
            raise ZoteroWebError(
                f"Zotero 请求失败（HTTP {response.status_code}）：{text}"
            )
        return response

    async def verify(self) -> dict[str, Any]:
        payload = (await self.request("GET", "/keys/current")).json()
        return payload if isinstance(payload, dict) else {}

    async def list_all(
        self, path: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        start = 0
        while True:
            query = {
                **(params or {}),
                "limit": 100,
                "start": start,
                "format": "json",
            }
            response = await self.request("GET", f"{self.prefix}/{path}", params=query)
            batch = response.json()
            if not isinstance(batch, list):
                raise ZoteroWebError("Zotero 返回了无法识别的列表数据")
            results.extend(item for item in batch if isinstance(item, dict))
            total = int(response.headers.get("Total-Results") or len(results))
            start += len(batch)
            if not batch or start >= total:
                break
        return results

    async def create_item(self, item: dict[str, Any]) -> tuple[str, int]:
        response = await self.request(
            "POST", f"{self.prefix}/items", json=[item]
        )
        results = _write_results(response.json())
        if not results:
            raise ZoteroWebError("Zotero 没有返回新建条目的 key")
        return results[0]

    async def patch_item(
        self, key: str, payload: dict[str, Any], version: int
    ) -> int:
        headers = (
            {"If-Unmodified-Since-Version": str(version)} if version else {}
        )
        response = await self.request(
            "PATCH", f"{self.prefix}/items/{key}", json=payload, headers=headers
        )
        return int(response.headers.get("Last-Modified-Version") or version)

    async def create_collection(self, name: str) -> tuple[str, int]:
        response = await self.request(
            "POST",
            f"{self.prefix}/collections",
            json=[{"name": name, "parentCollection": False}],
        )
        results = _write_results(response.json())
        if not results:
            raise ZoteroWebError("Zotero 没有返回新建分类的 key")
        return results[0]

    async def patch_collection(
        self, key: str, name: str, version: int
    ) -> int:
        headers = (
            {"If-Unmodified-Since-Version": str(version)} if version else {}
        )
        response = await self.request(
            "PATCH",
            f"{self.prefix}/collections/{key}",
            json={"name": name},
            headers=headers,
        )
        return int(response.headers.get("Last-Modified-Version") or version)

    async def download_file(self, attachment_key: str) -> bytes:
        url = f"{self.base}{self.prefix}/items/{attachment_key}/file"
        response = await self._client.get(url, headers=self.headers)
        if response.is_redirect:
            location = response.headers.get("location")
            if not location:
                raise ZoteroWebError("附件的下载地址为空")
            # S3 的预签名地址不需要（也不能带）Zotero 的鉴权头。
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(120.0, connect=20.0),
                follow_redirects=True,
            ) as plain:
                file_response = await plain.get(location)
                file_response.raise_for_status()
                return file_response.content
        if response.status_code >= 400:
            raise ZoteroWebError(
                f"下载附件失败（HTTP {response.status_code}）"
            )
        return response.content

    async def upload_pdf(
        self, parent_key: str, data: bytes, filename: str
    ) -> str:
        """按 Zotero Web API 的完整上传流程给条目挂一个 PDF。"""
        attachment = {
            "itemType": "attachment",
            "parentItem": parent_key,
            "linkMode": "imported_file",
            "title": "Full Text PDF",
            "contentType": "application/pdf",
            "filename": filename or "paper.pdf",
            "tags": [],
            "relations": {},
        }
        attachment_key, _ = await self.create_item(attachment)
        md5 = hashlib.md5(data).hexdigest()
        mtime = int(time.time() * 1000)
        authorization = await self.request(
            "POST",
            f"{self.prefix}/items/{attachment_key}/file",
            params={
                "md5": md5,
                "filename": filename or "paper.pdf",
                "filesize": len(data),
                "mtime": mtime,
                "params": 1,
            },
            headers={"If-None-Match": "*"},
        )
        payload = authorization.json()
        if payload.get("exists"):
            return attachment_key
        upload_key = payload.get("uploadKey")
        if not payload.get("url") or not upload_key:
            raise ZoteroWebError("Zotero 没有返回文件上传地址")

        if isinstance(payload.get("params"), list):
            files = [
                (str(part.get("name")), (None, str(part.get("value"))))
                for part in payload["params"]
                if isinstance(part, dict) and part.get("name")
            ]
            files.append(("file", (filename or "paper.pdf", data, "application/pdf")))
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(180.0, connect=20.0)
            ) as uploader:
                response = await uploader.post(payload["url"], files=files)
        else:
            body = (
                str(payload.get("prefix") or "").encode()
                + data
                + str(payload.get("suffix") or "").encode()
            )
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(180.0, connect=20.0)
            ) as uploader:
                response = await uploader.post(
                    payload["url"],
                    content=body,
                    headers={
                        "Content-Type": str(
                            payload.get("contentType")
                            or "multipart/form-data"
                        )
                    },
                )
        if response.status_code >= 400:
            raise ZoteroWebError(
                f"上传 PDF 失败（HTTP {response.status_code}）"
            )

        await self.request(
            "POST",
            f"{self.prefix}/items/{attachment_key}/file",
            headers={"If-None-Match": "*"},
            data={"upload": upload_key},
        )
        return attachment_key


def _folder_map(store) -> dict[str, dict[str, Any]]:
    return {
        str(folder.get("zotero_key")): folder
        for folder in store.list_folders()
        if folder.get("zotero_key")
    }


def _folder_for_collections(
    collections: list[str], by_key: dict[str, dict[str, Any]]
) -> tuple[str | None, str]:
    for key in collections:
        folder = by_key.get(key)
        if folder:
            return str(folder["id"]), key
    return None, ""


async def sync_library(
    data_dir,
    store,
    schedule_parse: Callable[[str], None],
) -> dict[str, Any]:
    """执行一次双向同步，返回可以直接展示给用户的统计。"""
    config = load_config(data_dir)
    if not config.get("api_key") or not config.get("user_id"):
        raise ZoteroWebError("还没有配置 Zotero Web API，请先填写 API Key 和用户 ID")
    if config.get("library_type") == "group" and not config.get("group_id"):
        raise ZoteroWebError("群组库需要填写群组 ID")

    report: dict[str, Any] = {
        "href": "started",
        "started_at": time.time(),
        "finished_at": 0,
        "added": 0,
        "updated": 0,
        "pushed": 0,
        "pdf_uploaded": 0,
        "pdf_downloaded": 0,
        "collections_pulled": 0,
        "collections_pushed": 0,
        "membership_pushed": 0,
        "membership_pulled": 0,
        "remote_deleted": 0,
        "skipped": 0,
        "errors": [],
    }
    errors: list[str] = report["errors"]

    def note_error(message: str) -> None:
        if len(errors) < 30:
            errors.append(message)

    async with ZoteroWebClient(config) as client:
        # ── 1. 拉取分类并映射到本地文件夹 ─────────────────
        try:
            remote_collections = await client.list_all("collections")
        except Exception as error:  # 网络不可用时直接失败
            raise ZoteroWebError(str(error)) from error

        for collection in remote_collections:
            key = str(collection.get("key") or "")
            data = collection.get("data") or {}
            version = int(collection.get("version") or data.get("version") or 0)
            name = str(data.get("name") or "").strip() or "未命名分类"
            if not key:
                continue
            folder = store.find_folder_by_zotero(key)
            if folder is None:
                existing = next(
                    (
                        row
                        for row in store.list_folders()
                        if str(row.get("name") or "").casefold() == name.casefold()
                    ),
                    None,
                )
                try:
                    if existing is not None:
                        store.update_folder(
                            existing["id"],
                            zotero_key=key,
                            zotero_name=name,
                            zotero_version=version,
                        )
                        report["collections_pulled"] += 1
                    else:
                        store.create_folder(
                            name,
                            zotero_key=key,
                            zotero_name=name,
                        )
                        report["collections_pulled"] += 1
                except Exception as error:
                    note_error(f"分类「{name}」同步失败：{error}")
                continue

            snapshot = str(folder.get("zotero_name") or "")
            # 本地改过名（name != 上次同步名）时以本地为准，等推送阶段处理。
            if name != folder.get("name") and snapshot == folder.get("name"):
                try:
                    store.rename_folder(folder["id"], name)
                except Exception as error:
                    note_error(f"分类「{name}」重命名失败：{error}")
            store.update_folder(
                folder["id"],
                zotero_name=name,
                zotero_version=version,
            )

        # ── 2. 把本地新建的文件夹推成 Zotero 分类 ─────────
        for folder in store.list_folders():
            if folder.get("zotero_key"):
                continue
            try:
                key, version = await client.create_collection(
                    str(folder.get("name") or "未命名分类")
                )
                store.update_folder(
                    folder["id"],
                    zotero_key=key,
                    zotero_name=folder.get("name"),
                    zotero_version=version,
                )
                report["collections_pushed"] += 1
            except Exception as error:
                note_error(f"分类「{folder.get('name')}」推送失败：{error}")

        folders_by_key = _folder_map(store)

        # ── 3. 拉取全部条目（含子附件）────────────────────
        try:
            remote_items = await client.list_all(
                "items", {"includeTrash": "0"}
            )
        except Exception as error:
            raise ZoteroWebError(str(error)) from error

        children: dict[str, list[dict[str, Any]]] = {}
        tops: list[dict[str, Any]] = []
        for item in remote_items:
            data = item.get("data") or {}
            parent = data.get("parentItem")
            if parent:
                children.setdefault(str(parent), []).append(item)
            elif str(data.get("itemType") or "") in REGULAR_TYPES:
                tops.append(item)

        rows = [row for row in store.list() if not row.get("project_id")]
        rows_by_key = {
            str(row.get("zotero_key")): row
            for row in rows
            if row.get("zotero_key")
        }
        rows_by_source = {
            str(row.get("source_id")): row
            for row in rows
            if row.get("source_id")
        }
        remote_keys = {str(item.get("key") or "") for item in remote_items}

        # ── 4. 远端 → 本地 ───────────────────────────────
        for item in tops:
            key = str(item.get("key") or "")
            data = item.get("data") or {}
            version = int(item.get("version") or data.get("version") or 0)
            if not key:
                continue
            row = rows_by_key.get(key) or rows_by_source.get(f"zotero:{key}")
            pdf_child = _pick_pdf(children.get(key) or [])
            meta = _meta_from_item(data)
            remote_cols = [
                str(value) for value in (data.get("collections") or [])
            ]

            if row is None:
                if pdf_child is None:
                    report["skipped"] += 1
                    continue
                try:
                    blob = await client.download_file(
                        str(pdf_child.get("key") or "")
                    )
                except Exception as error:
                    note_error(f"《{meta['title']}》PDF 下载失败：{error}")
                    report["skipped"] += 1
                    continue
                attachment_data = pdf_child.get("data") or {}
                filename = str(
                    attachment_data.get("filename")
                    or f"{meta['title'][:60]}.pdf"
                )
                if not filename.lower().endswith(".pdf"):
                    filename = f"{filename}.pdf"
                folder_id, folder_key = _folder_for_collections(
                    remote_cols, folders_by_key
                )
                try:
                    new_row = store.add(
                        filename,
                        blob,
                        {
                            **meta,
                            "source": "zotero",
                            "source_id": f"zotero:{key}",
                            "zotero_key": key,
                            "zotero_version": version,
                            "zotero_collections": remote_cols,
                            "zotero_folder_key": folder_key,
                            "zotero_pdf_key": str(pdf_child.get("key") or ""),
                            **({"folder_id": folder_id} if folder_id else {}),
                        },
                    )
                    schedule_parse(str(new_row["id"]))
                    report["added"] += 1
                    report["pdf_downloaded"] += 1
                except Exception as error:
                    note_error(f"《{meta['title']}》导入失败：{error}")
                continue

            # 已关联的条目：补齐同步元数据 + 远端元数据更新
            patch: dict[str, Any] = {}
            if not row.get("zotero_key"):
                patch["zotero_key"] = key
            if not row.get("zotero_pdf_key") and pdf_child is not None:
                patch["zotero_pdf_key"] = str(pdf_child.get("key") or "")

            local_version = int(row.get("zotero_version") or 0)
            if version > local_version:
                changed = {
                    name: value
                    for name, value in meta.items()
                    if str(row.get(name) or "") != value
                }
                if changed:
                    patch.update(changed)
                    report["updated"] += 1
                patch["zotero_version"] = version

            # 文件夹归属：本地改过就推送，远端改过就拉取
            prev_key = str(row.get("zotero_folder_key") or "")
            prev_cols = {str(value) for value in row.get("zotero_collections") or []}
            local_folder = (
                store.get_folder(row["folder_id"])
                if row.get("folder_id")
                else None
            )
            local_key = str((local_folder or {}).get("zotero_key") or "")
            remote_set = set(remote_cols)
            folder_key_after = local_key
            try:
                if local_key != prev_key:
                    desired = (remote_set - {prev_key}) | (
                        {local_key} if local_key else set()
                    )
                    if desired != remote_set:
                        await client.patch_item(
                            key, {"collections": sorted(desired)}, version
                        )
                        remote_set = desired
                        report["membership_pushed"] += 1
                elif remote_set != prev_cols:
                    folder_id, folder_key = _folder_for_collections(
                        remote_cols, folders_by_key
                    )
                    if (row.get("folder_id") or None) != folder_id:
                        store.move_paper(str(row["id"]), folder_id)
                        report["membership_pulled"] += 1
                    folder_key_after = folder_key
            except Exception as error:
                note_error(f"《{meta['title']}》分类归属同步失败：{error}")

            patch["zotero_collections"] = sorted(remote_set)
            patch["zotero_folder_key"] = folder_key_after
            store.update(str(row["id"]), **patch)

        # ── 5. 本地 → 远端（新建论文 + PDF）──────────────
        for row in store.list():
            if row.get("project_id"):
                continue
            # 上次推送成功但 PDF 上传失败的条目，这次补传。
            if row.get("zotero_key") and not row.get("zotero_pdf_key"):
                pdf = store.pdf_path(str(row["id"]))
                if pdf is not None:
                    try:
                        attachment_key = await client.upload_pdf(
                            str(row["zotero_key"]),
                            pdf.read_bytes(),
                            str(row.get("filename") or pdf.name),
                        )
                        store.update(
                            str(row["id"]), zotero_pdf_key=attachment_key
                        )
                        report["pdf_uploaded"] += 1
                    except Exception as error:
                        note_error(
                            f"《{row.get('title') or row['id']}》PDF 补传失败：{error}"
                        )
                continue
            if row.get("zotero_key"):
                continue
            if str(row.get("source_id") or "").startswith("zotero:"):
                # 从本地 Zotero 导入但没有可下载 PDF 的条目，避免重复推送。
                continue
            pdf = store.pdf_path(str(row["id"]))
            if pdf is None:
                report["skipped"] += 1
                continue
            folder = (
                store.get_folder(row["folder_id"])
                if row.get("folder_id")
                else None
            )
            collection_key = str((folder or {}).get("zotero_key") or "")
            payload = _item_payload(
                row, [collection_key] if collection_key else []
            )
            try:
                key, version = await client.create_item(payload)
            except Exception as error:
                note_error(
                    f"《{row.get('title') or row['id']}》推送到 Zotero 失败：{error}"
                )
                continue
            attachment_key = ""
            try:
                attachment_key = await client.upload_pdf(
                    key,
                    pdf.read_bytes(),
                    str(row.get("filename") or pdf.name),
                )
                report["pdf_uploaded"] += 1
            except Exception as error:
                note_error(
                    f"《{row.get('title') or row['id']}》PDF 上传失败：{error}"
                )
            store.update(
                str(row["id"]),
                zotero_key=key,
                zotero_version=version,
                zotero_collections=(
                    [collection_key] if collection_key else []
                ),
                zotero_folder_key=collection_key,
                zotero_pdf_key=attachment_key,
            )
            report["pushed"] += 1

        # ── 6. 远端已删除：只统计，不自动删本地 ───────────
        linked_keys = {
            str(row.get("zotero_key"))
            for row in store.list()
            if row.get("zotero_key")
        }
        report["remote_deleted"] = len(linked_keys - remote_keys)

    # ── 记录同步结果 ─────────────────────────────────────
    report["finished_at"] = time.time()
    report["ok"] = True
    config["last_sync"] = report["finished_at"]
    config["last_report"] = report
    save_config(data_dir, config)
    return report
