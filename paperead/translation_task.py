"""全文翻译的持久化任务模型与自适应调度。

三层职责：
- **任务状态**：``translation_job.json`` 记录 running / paused / partial 等状态，
  HTTP 连接消失后任务仍可继续；进程重启后转为 paused，用户带模型配置即可续跑。
- **自适应并发**：根据 rate-limit / 网络错误动态降并发，连续成功再缓慢恢复，
  避免固定 8 并发把端点打挂。
- **长块切分**：超长正文按段落/句子切成多段送模型，避免单块超上下文后整块失败。
"""

from __future__ import annotations

import asyncio
import json
import random
import time
import uuid
from pathlib import Path
from typing import Any

ACTIVE_STATUSES = {"queued", "running", "paused"}
TERMINAL_STATUSES = {"succeeded", "partial", "failed", "cancelled"}
FATAL_CODES = {"auth", "quota"}
RETRYABLE_CODES = {"rate_limit", "network", "timeout", "empty_content", "context", "unknown"}
DEFAULT_MAX_CHARS = 3000


def new_job(
    paper_id: str,
    lang: str,
    *,
    pages: list[int] | None = None,
    block_ids: list[str] | None = None,
    total: int = 0,
) -> dict[str, Any]:
    now = time.time()
    return {
        "job_id": uuid.uuid4().hex[:12],
        "paper_id": paper_id,
        "lang": lang,
        "status": "queued",
        "pages": pages,
        "block_ids": block_ids,
        "total": int(total),
        "done": 0,
        "failed": 0,
        "skipped": 0,
        "current_page": 0,
        "stage": "",
        "stage_done": 0,
        "stage_total": 0,
        "created_at": int(now),
        "updated_at": int(now),
        "started_at": 0,
        "finished_at": 0,
        "error": None,
        "report": None,
        "translated_ids": [],
        "published_ids": [],
        "failures": [],
        "cancel_requested": False,
        "resume_available": False,
        "concurrency": 0,
    }


def job_path(data_dir: Path | str, paper_id: str, lang: str) -> Path:
    return Path(data_dir) / "papers" / f"{paper_id}_translated" / lang / "translation_job.json"


def read_job(data_dir: Path | str, paper_id: str, lang: str) -> dict | None:
    try:
        data = json.loads(job_path(data_dir, paper_id, lang).read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) and data.get("job_id") else None


def write_job(data_dir: Path | str, job: dict) -> Path:
    job["updated_at"] = int(time.time())
    path = job_path(data_dir, job["paper_id"], job["lang"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(job, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    return path


def iter_job_paths(data_dir: Path | str):
    return Path(data_dir).glob("papers/*_translated/*/translation_job.json")


def published_ids(job: dict) -> list[str]:
    return list(job.get("published_ids") or [])


def public_job(job: dict, *, blocks: list[dict] | None = None, since: int = 0) -> dict:
    """对外返回的精简任务状态，不暴露完整 translated_ids。"""
    ids = list(job.get("published_ids") or job.get("translated_ids") or [])
    payload = {
        "job_id": job.get("job_id"),
        "paper_id": job.get("paper_id"),
        "lang": job.get("lang"),
        "status": job.get("status"),
        "pages": job.get("pages"),
        "total": int(job.get("total") or 0),
        "done": int(job.get("done") or 0),
        "failed": int(job.get("failed") or 0),
        "skipped": int(job.get("skipped") or 0),
        "current_page": int(job.get("current_page") or 0),
        # 外部引擎（BabelDOC）自己的阶段进度：block 数与引擎段落数没有一一对应，
        # 所以 PDF 全文翻译的进度展示只用这组字段。
        "stage": job.get("stage") or "",
        "stage_done": int(job.get("stage_done") or 0),
        "stage_total": int(job.get("stage_total") or 0),
        "created_at": job.get("created_at") or 0,
        "updated_at": job.get("updated_at") or 0,
        "started_at": job.get("started_at") or 0,
        "finished_at": job.get("finished_at") or 0,
        "error": job.get("error"),
        "report": job.get("report"),
        "resume_available": bool(job.get("resume_available")),
        "concurrency": int(job.get("concurrency") or 0),
        "next_cursor": len(ids),
        "blocks": blocks or [],
    }
    payload["pending"] = max(0, payload["total"] - payload["done"] - payload["failed"])
    return payload


class FatalTranslationError(RuntimeError):
    """认证/额度类错误：任务应立即停止，不能逐块重试。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class AdaptiveLimiter:
    """可动态伸缩的异步并发闸门。

    降并发时已经在跑的任务不打断，只让新任务等待；连续成功 8 次恢复 1 个并发。
    """

    def __init__(
        self,
        initial: int = 8,
        *,
        min_limit: int = 2,
        max_limit: int = 12,
    ) -> None:
        self.limit = max(min_limit, min(initial, max_limit))
        self.min_limit = min_limit
        self.max_limit = max_limit
        self.active = 0
        self.successes = 0
        self._cond = asyncio.Condition()

    async def acquire(self) -> None:
        async with self._cond:
            while self.active >= self.limit:
                await self._cond.wait()
            self.active += 1

    async def release(self) -> None:
        async with self._cond:
            self.active = max(0, self.active - 1)
            self._cond.notify_all()

    async def __aenter__(self) -> "AdaptiveLimiter":
        await self.acquire()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.release()

    def on_success(self) -> None:
        self.successes += 1
        if self.successes >= 8 and self.limit < self.max_limit:
            self.limit += 1
            self.successes = 0

    def on_error(self, code: str) -> None:
        self.successes = 0
        if code in {"rate_limit", "network", "timeout"}:
            self.limit = max(self.min_limit, self.limit // 2)


def retry_delay(attempt: int, code: str) -> float:
    """指数退避 + 抖动；限流时至少等 3 秒。"""
    base = 0.8 * (2 ** max(0, attempt))
    if code == "rate_limit":
        base = max(base, 3.0)
    elif code == "network":
        base = max(base, 1.2)
    return min(15.0, base + random.uniform(0, 0.4))


def split_text(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> list[str]:
    """把超长正文切成若干段，优先按段落、其次句末标点。"""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    parts: list[str] = []
    current = ""
    for para in text.split("\n\n"):
        block = para.strip()
        if not block:
            continue
        if len(block) > max_chars:
            # 段落本身超长：按句末标点再切，仍不行就硬切
            sentence = ""
            for piece in _sentence_split(block):
                if len(sentence) + len(piece) <= max_chars:
                    sentence += piece
                else:
                    if sentence:
                        parts.append(sentence)
                    sentence = piece
            if sentence:
                parts.append(sentence)
            continue
        if len(current) + len(block) + 2 <= max_chars:
            current = f"{current}\n\n{block}".strip()
        else:
            if current:
                parts.append(current)
            current = block
    if current:
        parts.append(current)

    out: list[str] = []
    for part in parts:
        if len(part) <= max_chars:
            out.append(part)
        else:
            out.extend(part[i : i + max_chars] for i in range(0, len(part), max_chars))
    return out or [text[:max_chars]]


def _sentence_split(text: str) -> list[str]:
    out: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in ".!?。！？" and len(buf) >= 20:
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return out


__all__ = [
    "ACTIVE_STATUSES",
    "TERMINAL_STATUSES",
    "FATAL_CODES",
    "RETRYABLE_CODES",
    "AdaptiveLimiter",
    "FatalTranslationError",
    "iter_job_paths",
    "job_path",
    "new_job",
    "public_job",
    "read_job",
    "retry_delay",
    "published_ids",
    "split_text",
    "write_job",
]