"""全文翻译的可译清单、块状态与验收报告。

为什么单独一层
--------------
旧实现里「哪些块可译」只存在于流式接口的临时过滤条件中：
前端自己按 ``translatable`` 算 pending，服务端又额外用 ``_is_translatable``
过滤公式碎片，两边口径不一致，于是出现「一直显示还有待译、再点一次却 0 块」。
这里把口径收敛成一次计算结果，供清单接口、翻译任务和构建守卫共用。

三个事实：
- ``manifest``：每个块是 ``done / pending / failed / skipped``，以及跳过原因；
- ``report``：一次翻译任务的覆盖率、失败清单、分页统计；
- ``coverage_guard``：构建译文 PDF 前的最低门槛，0 覆盖绝不允许产出。
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from .translate import _is_translatable

# 这些块按产品规则保留原文，不计入覆盖率分母。
EXCLUDED_KINDS = (
    "reference",
    "author",
    "affiliation",
    "figure",
    "table",
    "equation",
    "footnote",
    "margin",
)

# 覆盖率低于该值时，产物仍可用，但报告标 partial，前端提示用户。
PARTIAL_THRESHOLD = 0.6

STATUS_DONE = "done"
STATUS_PENDING = "pending"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"


def _parse_hash(blocks: list[dict]) -> str:
    """只对「块 id + 原文」取哈希，译文变化不会让清单失效。"""
    raw = "\n".join(
        f"{b.get('id') or ''}\t{(b.get('text') or '')}" for b in blocks
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def block_skip_reason(block: dict) -> str:
    """返回跳过原因；空字符串表示该块可翻译。"""
    text = (block.get("text") or "").strip()
    kind = str(block.get("kind") or "para")
    if not text:
        return "empty"
    if not (block.get("translatable") or kind == "caption"):
        return kind if kind in EXCLUDED_KINDS else "excluded"
    if not _is_translatable(text):
        return "fragment"
    return ""


def is_eligible(block: dict) -> bool:
    return not block_skip_reason(block)


def _failure_index(report: dict | None) -> dict[str, dict]:
    """把上一次任务的失败块按 id 建索引；已译块不受旧失败影响。"""
    if not isinstance(report, dict):
        return {}
    out: dict[str, dict] = {}
    for item in report.get("failures") or []:
        if not isinstance(item, dict):
            continue
        bid = str(item.get("block_id") or "")
        if bid:
            out[bid] = item
    return out


def build_manifest(
    blocks: list[dict],
    paper_id: str = "",
    lang: str = "zh-Hans",
    report: dict | None = None,
    pages: set[int] | None = None,
) -> dict[str, Any]:
    """生成块级状态清单。``report`` 用于把上次失败延续成 failed 状态。"""
    failures = _failure_index(report)
    rows: list[dict] = []
    eligible_ids: list[str] = []
    excluded: dict[str, int] = {}
    status_counts = {
        STATUS_DONE: 0,
        STATUS_PENDING: 0,
        STATUS_FAILED: 0,
        STATUS_SKIPPED: 0,
    }

    for block in blocks:
        page = int(block.get("page") or 0)
        if pages is not None and page not in pages:
            continue
        bid = str(block.get("id") or "")
        reason = block_skip_reason(block)
        if reason:
            status = STATUS_SKIPPED
            excluded[reason] = excluded.get(reason, 0) + 1
            rows.append(
                {"id": bid, "page": page, "status": status, "reason": reason}
            )
        else:
            eligible_ids.append(bid)
            if (block.get("translated") or "").strip():
                status = STATUS_DONE
                rows.append({"id": bid, "page": page, "status": status})
            elif bid in failures:
                failure = failures[bid]
                status = STATUS_FAILED
                rows.append(
                    {
                        "id": bid,
                        "page": page,
                        "status": status,
                        "code": failure.get("code") or "unknown",
                        "message": failure.get("message") or "",
                    }
                )
            else:
                status = STATUS_PENDING
                rows.append({"id": bid, "page": page, "status": status})
        status_counts[status] += 1

    eligible_total = len(eligible_ids)
    translated = status_counts[STATUS_DONE]
    coverage = translated / eligible_total if eligible_total else 0.0
    return {
        "paper_id": paper_id,
        "lang": lang,
        "parse_hash": _parse_hash(blocks),
        "page_count": max((int(b.get("page") or 0) for b in blocks), default=0),
        "blocks_total": len(blocks),
        "eligible_total": eligible_total,
        "eligible_ids": eligible_ids,
        "translated": translated,
        "failed": status_counts[STATUS_FAILED],
        "pending": status_counts[STATUS_PENDING],
        "skipped": status_counts[STATUS_SKIPPED],
        "coverage": round(coverage, 4),
        "status_counts": status_counts,
        "excluded": dict(sorted(excluded.items())),
        "blocks": rows,
    }


def build_report(
    manifest: dict,
    *,
    started_at: float | None = None,
    finished_at: float | None = None,
    new_translated: int = 0,
    failures: list[dict] | None = None,
    extra_failed: int = 0,
    usage: dict | None = None,
) -> dict[str, Any]:
    """根据最新的 manifest 生成一次任务的验收报告。

    ``failures`` 为空时用 ``extra_failed`` 计数（旧版 /translate 接口只返回
    失败数，没有单块明细）；两者都为空时从 manifest 推断。
    """
    now = time.time()
    finished = finished_at if finished_at is not None else now
    started = started_at if started_at is not None else finished

    failure_rows = list(failures or [])
    failed_total = (
        len(failure_rows) if failures is not None else max(extra_failed, manifest.get("failed", 0))
    )
    eligible = int(manifest.get("eligible_total") or 0)
    translated = int(manifest.get("translated") or 0)
    coverage = float(manifest.get("coverage") or 0.0)

    if eligible == 0:
        status = "blocked"
    elif translated == 0 and failed_total > 0:
        status = "failed"
    elif failed_total > 0 or coverage < PARTIAL_THRESHOLD:
        status = "partial"
    else:
        status = "succeeded"

    page_rows: dict[int, dict] = {}
    for row in manifest.get("blocks") or []:
        page = int(row.get("page") or 0)
        if row.get("status") == STATUS_SKIPPED:
            continue
        slot = page_rows.setdefault(
            page,
            {"page": page, "eligible": 0, "translated": 0, "failed": 0, "coverage": 0.0},
        )
        slot["eligible"] += 1
        if row.get("status") == STATUS_DONE:
            slot["translated"] += 1
        elif row.get("status") == STATUS_FAILED:
            slot["failed"] += 1
    for slot in page_rows.values():
        slot["coverage"] = (
            round(slot["translated"] / slot["eligible"], 4) if slot["eligible"] else 0.0
        )

    return {
        "paper_id": manifest.get("paper_id") or "",
        "lang": manifest.get("lang") or "zh-Hans",
        "parse_hash": manifest.get("parse_hash") or "",
        "status": status,
        "started_at": int(started),
        "finished_at": int(finished),
        "elapsed_ms": max(0, int((finished - started) * 1000)),
        "total": eligible,
        "translated": translated,
        "new_translated": int(new_translated),
        "failed": failed_total,
        "skipped": int(manifest.get("skipped") or 0),
        "pending": int(manifest.get("pending") or 0),
        "coverage": round(coverage, 4),
        "excluded": manifest.get("excluded") or {},
        "failures": failure_rows,
        "pages": [page_rows[p] for p in sorted(page_rows)],
        "usage": usage or {},
    }


def coverage_guard(manifest: dict) -> tuple[bool, str, str]:
    """构建译文 PDF 前的最低门槛。返回 ``(allowed, code, message)``。"""
    eligible = int(manifest.get("eligible_total") or 0)
    translated = int(manifest.get("translated") or 0)
    if eligible == 0:
        return (
            False,
            "no_eligible",
            "本篇没有可翻译的正文块（可能是扫描版、参考文献或解析未完成），无法生成译文 PDF。",
        )
    if translated == 0:
        failed = int(manifest.get("failed") or 0)
        suffix = f"（{failed} 块失败）" if failed else ""
        return (
            False,
            "zero_coverage",
            f"还没有完成任何正文翻译{suffix}，已拒绝生成与原文相同的空译文 PDF。",
        )
    return True, "", ""


def report_path(data_dir: Path | str, paper_id: str, lang: str) -> Path:
    return Path(data_dir) / "papers" / f"{paper_id}_translated" / lang / "translation_report.json"


def read_report(data_dir: Path | str, paper_id: str, lang: str) -> dict | None:
    path = report_path(data_dir, paper_id, lang)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def write_report(data_dir: Path | str, paper_id: str, lang: str, report: dict) -> Path:
    path = report_path(data_dir, paper_id, lang)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    return path


def classify_error(exc: BaseException | None) -> tuple[str, str]:
    """把模型/网络异常映射成稳定的错误码与用户可读文案。"""
    if exc is None:
        return "unknown", "翻译失败"

    names = {cls.__name__ for cls in type(exc).__mro__}
    raw = f"{type(exc).__name__}: {exc}".lower()

    def hit(*needles: str) -> bool:
        return any(needle in raw for needle in needles)

    if (
        {"AuthenticationError", "PermissionDeniedError"} & names
        or hit("401", "invalid api key", "unauthorized", "incorrect api key")
    ):
        return "auth", "模型认证失败：请检查 API Key、地址与模型权限。"
    if (
        "RateLimitError" in names
        or hit("429", "rate limit", "too many requests", "请求过于频繁")
    ):
        return "rate_limit", "模型限流：请求过快，请稍后重试。"
    if (
        {"APITimeoutError", "TimeoutError", "ReadTimeout", "ConnectTimeout"} & names
        or isinstance(exc, TimeoutError)
        or hit("timed out", "timeout", "超时")
    ):
        return "timeout", "模型响应超时，请检查网络后重试。"
    if (
        {"APIConnectionError", "ConnectError", "ReadError", "RemoteProtocolError"} & names
        or hit("connection error", "connection refused", "network", "连接失败")
    ):
        return "network", "连接模型服务失败：请检查网络或代理设置。"
    if hit("context length", "maximum context", "too many tokens", "上下文过长"):
        return "context", "单块内容超出模型上下文，需要切分后重试。"
    if hit("insufficient balance", "quota", "402", "余额", "额度"):
        return "quota", "模型账户额度不足。"
    if hit("空内容", "empty content", "empty response", "模型返回空"):
        return "empty_content", "模型返回空内容（可能是思考 token 占用了输出预算）。"
    return "unknown", f"翻译失败：{str(exc)[:160]}"


__all__ = [
    "EXCLUDED_KINDS",
    "PARTIAL_THRESHOLD",
    "STATUS_DONE",
    "STATUS_PENDING",
    "STATUS_FAILED",
    "STATUS_SKIPPED",
    "block_skip_reason",
    "build_manifest",
    "build_report",
    "classify_error",
    "coverage_guard",
    "is_eligible",
    "read_report",
    "report_path",
    "write_report",
]