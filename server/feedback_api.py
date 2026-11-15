"""问题反馈：把用户填写的标题、描述与截图提交到 Gitee Issue。

Gitee 开放 API 没有「Issue 附件上传」接口，唯一可用的附件接口只服务于发行版。
因此截图先通过「仓库内容」接口落到公开仓库的 `feedback-attachments/` 目录，
再用 raw 链接内嵌到 Issue 正文里，这样 Issue 页面能直接看到图片。

提交令牌的优先级：用户在设置里填写的令牌 > 环境变量 > 打包内置令牌文件。
内置令牌文件不进 git（见 .gitignore），只在构建安装包时随 sidecar 一起打进，
用户端开箱即用；用户也可以用自己的令牌覆盖掉它。
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

API_BASE = "https://gitee.com/api/v5"
OWNER = "deng_xiaohui"
REPO = "paperead"
BRANCH = "master"

TOKEN_ENV = "PAPEREAD_FEEDBACK_TOKEN"
SETTINGS_NAME = "feedback-settings.json"
TOKEN_ASSET = Path("server") / "resources" / "gitee-feedback-token.txt"

ATTACHMENT_DIR = "feedback-attachments"
MAX_IMAGES = 6
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_TITLE = 120
MAX_BODY = 20000

IMAGE_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


def _token_assets() -> Iterator[Path]:
    """内置令牌文件的候选位置：打包后的 sidecar 与源码目录。"""
    if getattr(sys, "frozen", False):
        bundle = getattr(sys, "_MEIPASS", None)
        if bundle:
            yield Path(bundle) / TOKEN_ASSET
        yield Path(sys.executable).resolve().parent / TOKEN_ASSET
    yield Path(__file__).resolve().parent / "resources" / "gitee-feedback-token.txt"


class FeedbackSettings:
    """用户自定义令牌的落盘读写；只存本机数据目录，不随安装包分发。"""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / SETTINGS_NAME

    def read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def user_token(self) -> str:
        token = self.read().get("token")
        return token.strip() if isinstance(token, str) else ""

    def save_user_token(self, token: str) -> None:
        payload = {**self.read(), "token": token, "updated_at": int(time.time())}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def clear_user_token(self) -> None:
        payload = self.read()
        payload.pop("token", None)
        payload["updated_at"] = int(time.time())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def resolve(self) -> tuple[str, str]:
        """返回 (令牌, 来源)；来源用于界面上说明当前用的是哪一份令牌。"""
        user = self.user_token()
        if user:
            return user, "user"
        env_token = os.getenv(TOKEN_ENV, "").strip()
        if env_token:
            return env_token, "env"
        for asset in _token_assets():
            try:
                token = asset.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if token:
                return token, "builtin"
        return "", "none"


class FeedbackImage(BaseModel):
    name: str | None = None
    type: str = "image/png"
    data: str


class FeedbackIn(BaseModel):
    title: str
    body: str = ""
    images: list[FeedbackImage] = []


class FeedbackTokenIn(BaseModel):
    token: str


def _decode_image(item: FeedbackImage, index: int) -> tuple[bytes, str]:
    mime = (item.type or "").split(";")[0].strip().lower()
    if mime not in IMAGE_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"第 {index} 张图片格式不支持，请使用 PNG / JPG / GIF / WebP",
        )
    text = (item.data or "").strip()
    if text.startswith("data:"):
        _, _, text = text.partition(",")
    try:
        raw = base64.b64decode(text, validate=False)
    except (binascii.Error, ValueError) as error:
        raise HTTPException(status_code=400, detail=f"第 {index} 张图片数据无法解析") from error
    if not raw:
        raise HTTPException(status_code=400, detail=f"第 {index} 张图片是空文件")
    if len(raw) > MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"第 {index} 张图片超过 {MAX_IMAGE_BYTES // (1024 * 1024)}MB",
        )
    return raw, mime


def _gitee_error(response: httpx.Response, fallback: str) -> HTTPException:
    message = ""
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        message = str(payload.get("message") or "").strip()
    return HTTPException(
        status_code=502,
        detail=f"{fallback}：{message or f'HTTP {response.status_code}'}",
    )


def _upload_image(
    client: httpx.Client,
    *,
    api_base: str,
    owner: str,
    repo: str,
    branch: str,
    token: str,
    raw: bytes,
    mime: str,
) -> str:
    ext = IMAGE_EXTENSIONS.get(mime, ".png")
    stamp = time.strftime("%Y%m%d%H%M%S")
    relative = f"{ATTACHMENT_DIR}/{time.strftime('%Y-%m')}/{stamp}-{uuid.uuid4().hex[:10]}{ext}"
    response = client.post(
        f"{api_base}/repos/{owner}/{repo}/contents/{relative}",
        data={
            "access_token": token,
            "content": base64.b64encode(raw).decode("ascii"),
            "message": f"feedback: add screenshot {relative.rsplit('/', 1)[-1]}",
            "branch": branch,
        },
    )
    if response.status_code not in (200, 201):
        raise _gitee_error(response, "截图上传失败")
    return f"https://gitee.com/{owner}/{repo}/raw/{branch}/{relative}"


def _create_issue(
    client: httpx.Client,
    *,
    api_base: str,
    owner: str,
    repo: str,
    token: str,
    title: str,
    body: str,
) -> dict[str, Any]:
    response = client.post(
        f"{api_base}/repos/{owner}/issues",
        data={
            "access_token": token,
            "repo": repo,
            "title": title,
            "body": body,
        },
    )
    if response.status_code not in (200, 201):
        raise _gitee_error(response, "创建 Issue 失败")
    try:
        payload = response.json()
    except ValueError as error:
        raise HTTPException(status_code=502, detail="Gitee 返回了无法解析的结果") from error
    if not isinstance(payload, dict) or not payload.get("html_url"):
        raise HTTPException(status_code=502, detail="Gitee 未返回 Issue 地址")
    return payload


def compose_body(text: str, image_urls: list[str]) -> str:
    parts: list[str] = []
    if text.strip():
        parts.append(text.strip())
    if image_urls:
        parts.append(f"### 截图（{len(image_urls)}）")
        parts.extend(f"![截图 {index}]({url})" for index, url in enumerate(image_urls, 1))
    return "\n\n".join(parts) or "（未填写描述）"


def build_feedback_router(
    data_dir: Path,
    *,
    api_base: str = API_BASE,
    owner: str = OWNER,
    repo: str = REPO,
    branch: str = BRANCH,
    transport: httpx.BaseTransport | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/feedback", tags=["feedback"])
    settings = FeedbackSettings(data_dir)

    def client() -> httpx.Client:
        return httpx.Client(
            timeout=httpx.Timeout(60.0, connect=15.0),
            follow_redirects=True,
            transport=transport,
        )

    def snapshot() -> dict[str, Any]:
        token, source = settings.resolve()
        return {
            "configured": bool(token),
            "source": source,
            "owner": owner,
            "repo": repo,
            "issue_page": f"https://gitee.com/{owner}/{repo}/issues",
            "max_images": MAX_IMAGES,
            "max_image_bytes": MAX_IMAGE_BYTES,
        }

    @router.get("/status")
    def feedback_status() -> dict[str, Any]:
        return snapshot()

    @router.post("")
    def submit_feedback(req: FeedbackIn) -> dict[str, Any]:
        title = (req.title or "").strip()
        body = (req.body or "").strip()
        if not title:
            raise HTTPException(status_code=400, detail="请填写问题标题")
        if len(title) > MAX_TITLE:
            raise HTTPException(status_code=400, detail=f"标题请控制在 {MAX_TITLE} 字以内")
        if len(body) > MAX_BODY:
            raise HTTPException(status_code=400, detail=f"描述请控制在 {MAX_BODY} 字以内")
        images = list(req.images or [])
        if len(images) > MAX_IMAGES:
            raise HTTPException(status_code=400, detail=f"最多上传 {MAX_IMAGES} 张图片")

        token, _ = settings.resolve()
        if not token:
            raise HTTPException(
                status_code=503,
                detail="尚未配置 Gitee 提交令牌，请先在下方填写借用你自己的令牌",
            )

        decoded = [_decode_image(item, index) for index, item in enumerate(images, 1)]

        with client() as http:
            urls: list[str] = []
            for index, (raw, mime) in enumerate(decoded, 1):
                try:
                    urls.append(
                        _upload_image(
                            http,
                            api_base=api_base,
                            owner=owner,
                            repo=repo,
                            branch=branch,
                            token=token,
                            raw=raw,
                            mime=mime,
                        )
                    )
                except httpx.RequestError as error:
                    raise HTTPException(
                        status_code=502,
                        detail=f"第 {index} 张截图上传失败：连接 Gitee 出错",
                    ) from error

            try:
                issue = _create_issue(
                    http,
                    api_base=api_base,
                    owner=owner,
                    repo=repo,
                    token=token,
                    title=title,
                    body=compose_body(body, urls),
                )
            except httpx.RequestError as error:
                raise HTTPException(status_code=502, detail="连接 Gitee 出错，请稍后重试") from error

        return {
            "issue_url": issue.get("html_url"),
            "number": str(issue.get("number") or ""),
            "images": len(urls),
        }

    @router.put("/token")
    def save_token(req: FeedbackTokenIn) -> dict[str, Any]:
        token = (req.token or "").strip()
        if len(token) < 16:
            raise HTTPException(status_code=400, detail="令牌格式不正确")
        try:
            with client() as http:
                response = http.get(f"{api_base}/user", params={"access_token": token})
        except httpx.RequestError as error:
            raise HTTPException(status_code=502, detail="连接 Gitee 出错，请稍后重试") from error
        if response.status_code != 200:
            raise _gitee_error(response, "令牌无效或没有访问权限")
        settings.save_user_token(token)
        return snapshot()

    @router.delete("/token")
    def clear_token() -> dict[str, Any]:
        settings.clear_user_token()
        return snapshot()

    return router