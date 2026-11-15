"""问题反馈 → Gitee Issue 的回归测试。

Gitee 的开放 API 没有 Issue 附件接口，截图走仓库内容接口再内嵌 raw 链接，
这里用 httpx.MockTransport 固化这条链路，避免测试真的往公开仓库提交东西。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import feedback_api
from server.feedback_api import build_feedback_router, compose_body

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


@pytest.fixture(autouse=True)
def _no_ambient_token(monkeypatch):
    """本机可能有构建用的内置令牌，测试里必须隔离掉。"""
    monkeypatch.delenv("PAPEREAD_FEEDBACK_TOKEN", raising=False)
    monkeypatch.setattr(feedback_api, "_token_assets", lambda: iter(()))


def _make(tmp_path: Path, handler, token: str | None = None) -> TestClient:
    if token:
        (tmp_path / "feedback-settings.json").write_text(
            json.dumps({"token": token}), encoding="utf-8"
        )
    router = build_feedback_router(
        tmp_path,
        api_base="https://gitee.test/api/v5",
        transport=httpx.MockTransport(handler),
    )
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _form(request: httpx.Request) -> dict[str, str]:
    parsed = parse_qs(request.content.decode("utf-8"))
    return {key: values[0] for key, values in parsed.items()}


def _ok_handler(seen: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/contents/feedback-attachments/2026-11/fixed.png") or (
            "/contents/feedback-attachments/" in request.url.path
        ):
            return httpx.Response(201, json={"content": {"sha": "abc"}})
        if request.url.path == "/api/v5/repos/deng_xiaohui/issues":
            return httpx.Response(
                201,
                json={
                    "number": "IKVG5L",
                    "html_url": "https://gitee.com/deng_xiaohui/paperead/issues/IKVG5L",
                },
            )
        return httpx.Response(404, json={"message": "not found"})

    return handler


def test_status_without_token(tmp_path: Path):
    client = _make(tmp_path, _ok_handler([]))
    payload = client.get("/api/feedback/status").json()
    assert payload["configured"] is False
    assert payload["source"] == "none"
    assert payload["owner"] == "deng_xiaohui"
    assert payload["issue_page"].endswith("/issues")


def test_submit_requires_token(tmp_path: Path):
    client = _make(tmp_path, _ok_handler([]))
    response = client.post("/api/feedback", json={"title": "打不开", "body": ""})
    assert response.status_code == 503


def test_submit_uploads_images_and_creates_issue(tmp_path: Path):
    seen: list[httpx.Request] = []
    client = _make(tmp_path, _ok_handler(seen), token="t" * 20)

    response = client.post(
        "/api/feedback",
        json={
            "title": "导出 PDF 页码错位",
            "body": "第 3 页开始页码 +1。",
            "images": [
                {
                    "name": "shot.png",
                    "type": "image/png",
                    "data": "data:image/png;base64," + base64.b64encode(PNG).decode(),
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["issue_url"] == "https://gitee.com/deng_xiaohui/paperead/issues/IKVG5L"
    assert payload["number"] == "IKVG5L"
    assert payload["images"] == 1

    upload, issue = seen[0], seen[1]
    assert "/repos/deng_xiaohui/paperead/contents/feedback-attachments/" in upload.url.path
    form = _form(upload)
    assert form["content"] == base64.b64encode(PNG).decode()
    assert form["branch"] == "master"

    issue_form = _form(issue)
    assert issue_form["title"] == "导出 PDF 页码错位"
    assert "第 3 页开始页码 +1。" in issue_form["body"]
    assert "https://gitee.com/deng_xiaohui/paperead/raw/master/feedback-attachments/" in issue_form["body"]
    assert issue_form["repo"] == "paperead"


def test_title_is_required(tmp_path: Path):
    client = _make(tmp_path, _ok_handler([]), token="t" * 20)
    response = client.post("/api/feedback", json={"title": "   ", "body": "x"})
    assert response.status_code == 400


def test_rejects_too_many_images(tmp_path: Path):
    client = _make(tmp_path, _ok_handler([]), token="t" * 20)
    image = {"type": "image/png", "data": base64.b64encode(PNG).decode()}
    response = client.post(
        "/api/feedback",
        json={"title": "x", "body": "", "images": [image] * 7},
    )
    assert response.status_code == 400


def test_rejects_unsupported_type(tmp_path: Path):
    client = _make(tmp_path, _ok_handler([]), token="t" * 20)
    response = client.post(
        "/api/feedback",
        json={
            "title": "x",
            "body": "",
            "images": [{"type": "application/pdf", "data": base64.b64encode(b"x").decode()}],
        },
    )
    assert response.status_code == 400


def test_reports_upload_failure(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "推送失败"})

    client = _make(tmp_path, handler, token="t" * 20)
    response = client.post(
        "/api/feedback",
        json={
            "title": "x",
            "body": "",
            "images": [{"type": "image/png", "data": base64.b64encode(PNG).decode()}],
        },
    )
    assert response.status_code == 502
    assert "推送失败" in response.json()["detail"]


def test_save_and_clear_token(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/user"):
            assert request.url.params.get("access_token") == "u" * 24
            return httpx.Response(200, json={"login": "deng_xiaohui"})
        return httpx.Response(404, json={"message": "not found"})

    client = _make(tmp_path, handler)
    saved = client.put("/api/feedback/token", json={"token": "u" * 24})
    assert saved.status_code == 200, saved.text
    assert saved.json()["configured"] is True
    assert saved.json()["source"] == "user"

    cleared = client.delete("/api/feedback/token")
    assert cleared.status_code == 200
    assert cleared.json()["configured"] is False


def test_invalid_token_is_rejected(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "登录失效"})

    client = _make(tmp_path, handler)
    response = client.put("/api/feedback/token", json={"token": "z" * 24})
    assert response.status_code == 502 or response.status_code == 401
    assert (tmp_path / "feedback-settings.json").is_file() is False


def test_compose_body_keeps_text_and_images():
    body = compose_body("复现步骤", ["https://example.com/a.png"])
    assert body.startswith("复现步骤")
    assert "### 截图（1）" in body
    assert "![截图 1](https://example.com/a.png)" in body
    assert compose_body("  ", []) == "（未填写描述）"