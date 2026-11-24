"""BabelDOC 翻译引擎接入。

为什么用独立引擎
----------------
BabelDOC 依赖 ``pymupdf<2``，主应用用的是 2.x，两者不能装进同一个
虚拟环境；同时 BabelDOC 进程峰值内存约 1.2GB，独立子进程崩了也不会拖垮
主服务。所以引擎是一个独立的 venv（``engines/babeldoc/<version>``），
由应用按需下载，与 App 版本解耦。

分工
----
- **BabelDOC**：解析版面、按段落翻译、保留公式/样式，产出
  ``il_translated.json``（中间表示）与排版后的 PDF；
- **本模块**：把 IL 里已翻译的段落按 bbox 映射回应用的 ``Block``，
  再把文字写回阅读页。表格交给应用自己的表格翻译保护，不写回。

坐标：IL 的 y 轴原点在页面底部，应用是左上原点，映射时统一翻转。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("paperead.babeldoc")

# 应用语言代码 → BabelDOC 的 lang-out
LANG_OUT = {
    "zh-Hans": "zh",
    "zh-Hant": "zh-TW",
    "en": "en",
    "ja": "ja",
    "ko": "ko",
    "fr": "fr",
    "de": "de",
    "es": "es",
    "ru": "ru",
}

ENGINE_NAME = "babeldoc"
# IL 里允许写回正文的段落类型；table/abandon/fallback_line 一律不用。
_ALLOWED_LABELS = {"title", "plain text", "figure_caption", "table_caption", "table_footnote"}
_PLACEHOLDER_RE = re.compile(r"\{\s*v\s*\d+\s*\}")
_STYLE_TAG_RE = re.compile(r"<\s*/?\s*style[^>]*>")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_PROGRESS_RE = re.compile(
    r"([A-Za-z][A-Za-z \-]*?)(?:\s*\(\s*\d+\s*/\s*\d+\s*\))?\s*-+\s*(\d+)/(\d+)"
)


class EngineError(RuntimeError):
    """引擎不可用或运行失败（调用方应回退到 LLM 分块翻译）。"""


class EngineCancelled(RuntimeError):
    """用户取消/任务中断：调用方按取消处理，不要当失败。"""


# ── 定位引擎 ───────────────────────────────────────────────────────────


def _data_root() -> Path:
    from server.runtime import default_data_dir

    return default_data_dir()


def engine_root() -> Path:
    return _data_root() / "engines" / ENGINE_NAME


def _repo_root() -> Path:
    # server/babeldoc_engine.py → 仓库根
    return Path(__file__).resolve().parent.parent


def _exe_in(home: Path) -> Path | None:
    for rel in ("Scripts/babeldoc.exe", "bin/babeldoc", "Scripts/babeldoc"):
        candidate = home / rel
        if candidate.is_file():
            return candidate
    return None


def _engine_candidates() -> list[Path]:
    out: list[Path] = []
    explicit = os.environ.get("PAPEREAD_BABELDOC_HOME", "").strip()
    if explicit:
        out.append(Path(explicit).expanduser())
        return out
    root = engine_root()
    if root.is_dir():
        # 版本目录按名称倒序，优先用较新的引擎
        try:
            children = sorted(root.iterdir(), reverse=True)
        except OSError:
            children = []
        for child in children:
            if child.is_dir():
                out.append(child)
    # 开发态：spike 期间已经装好的独立 venv（发布包里不存在）
    out.append(_repo_root().parent / ".refs" / "babeldoc-venv")
    out.append(_repo_root() / ".refs" / "babeldoc-venv")
    return out


def launcher(home: Path) -> list[str] | None:
    """返回启动 BabelDOC 的命令前缀。

    发布包里是便携 Python（``python/python.exe`` + ``run_babeldoc.py``），
    开发态是独立 venv 的 ``Scripts/babeldoc.exe``。两种形态都支持，
    但都不能依赖 Scripts 里写死旧绝对路径的 exe 启动器。
    """
    for rel in ("python/python.exe", "python/bin/python3", "python/bin/python"):
        python = home / rel
        runner = home / "run_babeldoc.py"
        if python.is_file() and runner.is_file():
            # -u：日志走管道时保持实时刷新，进度条与取消才能及时生效。
            return [str(python), "-u", str(runner)]
    exe = _exe_in(home)
    return [str(exe)] if exe is not None else None


def engine_home() -> Path | None:
    for home in _engine_candidates():
        try:
            if launcher(home) is not None:
                return home
        except OSError:
            continue
    return None


def cache_dir(home: Path | None = None) -> Path:
    """BabelDOC 的模型/字体缓存目录（首启下载一次，之后离线可用）。"""
    env = os.environ.get("BABELDOC_CACHE_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    home = home or engine_home()
    if home is not None:
        sibling = home.parent / "cache"
        if sibling.is_dir():
            return sibling
    dev = _repo_root().parent / ".refs" / "spike" / "cache"
    if dev.is_dir():
        return dev
    return (home.parent if home is not None else engine_root()) / "cache"


def status() -> dict[str, Any]:
    home = engine_home()
    cache = cache_dir(home) if home is not None else None
    cache_ready = False
    if cache is not None and cache.is_dir():
        try:
            cache_ready = any(cache.iterdir())
        except OSError:
            cache_ready = False
    return {
        "installed": home is not None,
        "home": str(home) if home is not None else "",
        "cache_dir": str(cache) if cache is not None else "",
        "cache_ready": cache_ready,
        "supported_langs": sorted(LANG_OUT),
    }


def available(lang: str | None = None) -> bool:
    # 离线联调（PAPEREAD_MOCK=1）用固定的假后端，真实引擎会绕过 mock 卡住测试。
    if os.environ.get("PAPEREAD_MOCK") == "1":
        return False
    if os.environ.get("PAPEREAD_BABELDOC", "1") == "0":
        return False
    if engine_home() is None:
        return False
    return lang is None or lang in LANG_OUT


# ── IL → Block 映射 ────────────────────────────────────────────────────


def _box(raw: dict) -> tuple[float, float, float, float]:
    return (float(raw["x"]), float(raw["y"]), float(raw["x2"]), float(raw["y2"]))


def _area(box: tuple[float, float, float, float]) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _intersection(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> float:
    w = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    h = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    return w * h


def _formula_text(item: dict) -> str:
    chars = (item.get("pdf_formula") or {}).get("pdf_character") or []
    return "".join(str(ch.get("char_unicode") or "") for ch in chars)


def _restore(text: str, composition: list[dict]) -> str:
    """把 ``{vN}`` 按出现顺序还原成 composition 里第 N 个公式的字符。

    占位符编号在 BabelDOC 内部是「段落内联元素 id」，并不连续；
    实测按出现顺序与 composition 中公式顺序一一对应，所以按序替换。
    """
    formulas = [
        _formula_text(item) for item in composition if item.get("pdf_formula") is not None
    ]
    index = 0

    def repl(match: re.Match) -> str:
        nonlocal index
        if index >= len(formulas):
            return match.group(0)
        value = formulas[index]
        index += 1
        return value

    out = _PLACEHOLDER_RE.sub(repl, text)
    out = _STYLE_TAG_RE.sub("", out)
    out = re.sub(r"[ \t]+", " ", out)
    # 中文字符两侧不需要空格；拉丁字母之间保留单个空格
    out = re.sub(r"\s+(?=[\u4e00-\u9fff，。；：、！？）】」》])", "", out)
    out = re.sub(r"(?<=[\u4e00-\u9fff，。；：、！？（【「《])\s+", "", out)
    return out.strip()


def _sentences(text: str) -> list[str]:
    out: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in "。！？；!?;":
            out.append(buf)
            buf = ""
    if buf.strip():
        out.append(buf)
    return [s for s in out if s.strip()] or [text]


def _split_by_weights(text: str, weights: list[float]) -> list[str]:
    """一个 IL 段落跨多个 Block 时，按重叠面积把译文按句子切开。"""
    if len(weights) <= 1 or sum(weights) <= 0:
        return [text] + [""] * max(0, len(weights) - 1)
    segs = _sentences(text)
    if len(segs) <= 1:
        return [text] + [""] * (len(weights) - 1)
    total = sum(weights)
    targets: list[float] = []
    acc = 0.0
    for weight in weights:
        acc += weight
        targets.append(acc / total * len(text))
    parts = [""] * len(weights)
    slot = 0
    consumed = 0
    for seg in segs:
        while slot < len(weights) - 1 and consumed + len(seg) / 2 > targets[slot]:
            slot += 1
        parts[slot] += seg
        consumed += len(seg)
    return parts


def _has_target_text(text: str, lang_out: str) -> bool:
    if lang_out in ("zh", "zh-TW"):
        return bool(_CJK_RE.search(text))
    return bool(re.search(r"[A-Za-z\u0400-\u04ff\u3040-\u30ff\uac00-\ud7af]", text))


def _norm_block_text(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def map_translations(
    il: dict,
    blocks: list[dict],
    lang: str,
    *,
    eligible_ids: set[str] | None = None,
) -> tuple[dict[str, str], dict[str, int]]:
    """把 IL 中的译文映射回 Block。

    Args:
        il: ``il_translated.json`` 解析结果。
        blocks: 应用里的块（dict，含 page / bbox / kind / text）。
        lang: 应用语言代码（zh-Hans 等）。
        eligible_ids: 只写这些块；None 表示按块自身字段判断。

    Returns:
        ``({block_id: 译文}, 统计)``；已有译文的块不会被覆盖。
    """
    lang_out = LANG_OUT.get(lang, "zh")
    stats = {
        "paragraphs": 0,
        "assigned": 0,
        "split": 0,
        "skipped": 0,
        "unmatched": 0,
        "blocks": 0,
        "filled": 0,
    }

    by_page: dict[int, list[dict]] = {}
    skip_kinds = {"table", "equation", "figure", "reference", "author", "affiliation", "margin", "footnote"}
    for block in blocks:
        if not block.get("bbox"):
            continue
        if str(block.get("kind") or "para") in skip_kinds:
            continue
        if eligible_ids is not None and str(block.get("id") or "") not in eligible_ids:
            continue
        if (block.get("translated") or "").strip():
            continue
        by_page.setdefault(int(block.get("page") or 0), []).append(block)

    collected: dict[str, list[tuple[float, float, str]]] = {}

    for page in il.get("page") or []:
        pno = int(page.get("page_number") or 0) + 1
        page_blocks = by_page.get(pno)
        if not page_blocks:
            continue
        crop = page.get("cropbox") or page.get("mediabox") or {}
        crop_box = crop.get("box") or {}
        top = float(crop_box.get("y2") or 0.0)

        def to_top_box(raw: dict) -> tuple[float, float, float, float]:
            box = _box(raw)
            return (box[0], top - box[3], box[2], top - box[1])

        # 表格区域里的文字不写回正文；表格由应用自己的 table_html 负责
        table_boxes = [
            to_top_box(item["box"])
            for item in page.get("page_layout") or []
            if str(item.get("class_name") or "").lower() == "table" and item.get("box")
        ]

        for para in page.get("pdf_paragraph") or []:
            if str(para.get("layout_label") or "") not in _ALLOWED_LABELS:
                continue
            raw_text = (para.get("unicode") or "").strip()
            if not raw_text or not para.get("box"):
                continue
            text = _restore(raw_text, para.get("pdf_paragraph_composition") or [])
            if not text or text.strip() in _ALLOWED_LABELS:
                continue
            if not _has_target_text(text, lang_out):
                stats["skipped"] += 1
                continue

            stats["paragraphs"] += 1
            mapped = to_top_box(para["box"])

            center = ((mapped[0] + mapped[2]) / 2, (mapped[1] + mapped[3]) / 2)
            inside_table = any(
                tb[0] <= center[0] <= tb[2] and tb[1] <= center[1] <= tb[3]
                for tb in table_boxes
            )
            if inside_table:
                stats["skipped"] += 1
                continue

            scored: list[tuple[float, dict]] = []
            for block in page_blocks:
                bbox = tuple(float(v) for v in block["bbox"])
                overlap = _intersection(mapped, bbox)
                if overlap <= 0:
                    continue
                ratio = overlap / max(_area(mapped), 1.0)
                if ratio >= 0.1:
                    scored.append((overlap, block))
            if not scored:
                for block in page_blocks:
                    bbox = tuple(float(v) for v in block["bbox"])
                    if bbox[0] <= center[0] <= bbox[2] and bbox[1] <= center[1] <= bbox[3]:
                        scored = [(1.0, block)]
                        break
            if not scored:
                stats["unmatched"] += 1
                continue

            scored.sort(key=lambda item: item[0], reverse=True)
            scored = scored[:3]
            stats["assigned"] += 1
            if len(scored) > 1:
                stats["split"] += 1
                parts = _split_by_weights(text, [w for w, _ in scored])
            else:
                parts = [text]

            for (_weight, block), part in zip(scored, parts):
                if part.strip():
                    collected.setdefault(str(block["id"]), []).append(
                        (mapped[1], mapped[0], part)
                    )

    result: dict[str, str] = {}
    for block in blocks:
        bid = str(block.get("id") or "")
        if bid not in collected:
            continue
        stats["blocks"] += 1
        items = sorted(collected[bid])
        joined = "\n".join(text for _y, _x, text in items).strip()
        # 极少数段落译文与原文完全相同（多为参考文献/邮件），不写回，
        # 留给兜底路径或保持英文，避免制造「假译文」。
        if not joined or _norm_block_text(joined) == _norm_block_text(block.get("text") or ""):
            continue
        result[bid] = joined
        stats["filled"] += 1
    return result, stats


# ── 运行引擎 ───────────────────────────────────────────────────────────


def _build_command(
    launcher_cmd: list[str],
    pdf: Path,
    out_dir: Path,
    work_dir: Path,
    cfg: dict,
    *,
    qps: float = 8,
) -> list[str]:
    lang_out = LANG_OUT.get(str(cfg.get("lang") or "zh-Hans"), "zh")
    return [
        *launcher_cmd,
        "--openai",
        "--openai-model",
        str(cfg.get("model") or ""),
        "--openai-base-url",
        str(cfg.get("base_url") or ""),
        "--openai-api-key",
        str(cfg.get("api_key") or ""),
        "--openai-thinking",
        "disabled",
        "--lang-in",
        "en",
        "--lang-out",
        lang_out,
        "--files",
        str(pdf),
        "--output",
        str(out_dir),
        "--working-dir",
        str(work_dir),
        "--qps",
        str(qps),
        "--report-interval",
        "2",
        "--skip-scanned-detection",
        "--watermark-output-mode",
        "no_watermark",
        "--debug",
    ]


def _parse_progress(line: str) -> tuple[float, str] | None:
    """从 rich 进度行里取出大致阶段进度，供任务状态展示。"""
    plain = _ANSI_RE.sub("", line).replace("\r", "\n").strip()
    matches = list(_PROGRESS_RE.finditer(plain))
    if not matches:
        return None
    match = matches[-1]
    stage = match.group(1).strip()
    done, total = int(match.group(2)), max(1, int(match.group(3)))
    ratio = min(1.0, done / total)
    lowered = stage.lower()
    if "translate paragraph" in lowered:
        return 0.12 + 0.76 * ratio, stage
    if "typesetting" in lowered:
        return 0.9 + 0.04 * ratio, stage
    if "save pdf" in lowered:
        return 0.96, stage
    if "parse" in lowered or "extract" in lowered or "layout" in lowered:
        return 0.02 + 0.08 * ratio, stage
    return None


def _save_pdf_copy(out_dir: Path, dest: Path, kind: str = "mono") -> Path | None:
    """把引擎产出的 mono/dual PDF 拷到 ``dest``（工作目录随后会被整个删掉）。"""
    try:
        candidates = [
            p
            for p in out_dir.rglob("*.pdf")
            if f".{kind}." in p.name and ".decompressed" not in p.name
        ]
    except OSError:
        return None
    if not candidates:
        return None
    # 优先 no_watermark；同条件下取文件名最短的一份（避免命中调试副本）
    candidates.sort(key=lambda p: (".no_watermark" not in p.name, len(p.name)))
    dest = Path(dest)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".tmp.pdf")
        shutil.copyfile(candidates[0], tmp)
        tmp.replace(dest)
    except OSError as exc:
        log.warning("保存 BabelDOC %s PDF 失败：%s", kind, exc)
        return None
    _strip_debug_drawings(dest)
    return dest


# BabelDOC 的 debug 模式在产出 IL 的同时，会把版面框画进 PDF（绿/蓝/橙/紫细线）。
# 这些属于调试信息，不能出现在交付给用户的译文里，这里按「孤立的描边矩形组」
# 精确删除：`q n R G B RG w w x y W H re S Q`，且颜色命中调试调色板。
_DEBUG_COLORS = {
    (0.204, 0.780, 0.349),
    (0.0, 0.478, 1.0),
    (0.188, 0.690, 0.780),
    (1.0, 0.584, 0.0),
    (0.345, 0.337, 0.839),
}
_RECT_GROUP_RE = re.compile(
    r"q\s+(?:n\s+)?(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+RG\s+([\d.]+)\s+w\s+"
    r"(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+re S"
    # 双语对照版里同一段内容会被重写成 `... re S <color> rg n Q`
    r"(?:\s+-?[\d.]+\s+-?[\d.]+\s+-?[\d.]+\s+rg\s+n)?\s*Q\s?"
)
# debug 模式还会把 "paragraph[xx][plain text]" 这类小字标签逐字画进页面，
# 形态固定为 `q R G B rg R G B RG BT...ET Q`，同样按调色板识别删除。
_DEBUG_TEXT_RE = re.compile(
    r"q\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+rg\s+"
    r"(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+RG\s+BT.*?ET Q\s?",
    re.S,
)


def _is_debug_color(r: float, g: float, b: float) -> bool:
    return (round(r, 3), round(g, 3), round(b, 3)) in _DEBUG_COLORS


def _strip_debug_drawings(path: Path) -> None:
    try:
        import pymupdf
    except Exception:  # pragma: no cover - 主应用一定带 pymupdf
        return
    try:
        doc = pymupdf.open(path)
    except Exception as exc:
        log.warning("打开 BabelDOC 产物失败，跳过调试框清理：%s", exc)
        return
    removed = 0

    def clean_stream(xref: int) -> None:
        nonlocal removed
        try:
            raw = doc.xref_stream(xref)
        except Exception:
            return
        text = raw.decode("latin-1")

        def rect_repl(match: re.Match) -> str:
            nonlocal removed
            try:
                r, g, b, width = (
                    float(match.group(1)),
                    float(match.group(2)),
                    float(match.group(3)),
                    float(match.group(4)),
                )
            except ValueError:
                return match.group(0)
            if _is_debug_color(r, g, b) and width <= 0.6:
                removed += 1
                return ""
            return match.group(0)

        cleaned = _RECT_GROUP_RE.sub(rect_repl, text)

        def text_repl(match: re.Match) -> str:
            nonlocal removed
            try:
                r, g, b = (
                    float(match.group(1)),
                    float(match.group(2)),
                    float(match.group(3)),
                )
            except ValueError:
                return match.group(0)
            if _is_debug_color(r, g, b):
                removed += 1
                return ""
            return match.group(0)

        cleaned = _DEBUG_TEXT_RE.sub(text_repl, cleaned)
        if cleaned != text:
            doc.update_stream(xref, cleaned.encode("latin-1"))

    try:
        for page in doc:
            for xref in page.get_contents():
                clean_stream(xref)
        # 双语对照版把两页拼成 Form XObject，调试框藏在表单流里，也要清一遍。
        for xref in range(1, doc.xref_length()):
            try:
                kind = doc.xref_get_key(xref, "Subtype")
            except Exception:
                continue
            if kind and kind[1] == "/Form":
                clean_stream(xref)
        if removed:
            tmp = path.with_suffix(".clean.pdf")
            doc.save(tmp, garbage=3, deflate=True)
            doc.close()
            tmp.replace(path)
            log.info("已移除 BabelDOC 调试框：%s 个", removed)
            return
    finally:
        if not doc.is_closed:
            doc.close()


async def run(
    pdf: Path,
    cfg: dict,
    *,
    on_progress: Callable[[float, str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    timeout: float = 1800.0,
    qps: float = 8,
    save_pdf: Path | None = None,
) -> tuple[dict, dict[str, int]]:
    """用引擎翻译一篇 PDF，返回 ``(il, stats)``。

    Args:
        cfg: ``{"api_key", "base_url", "model", "lang"}``。
        on_progress: ``(0~1, 阶段名)`` 回调，用于刷新任务进度。
        should_cancel: 轮询取消标记；返回 True 时立刻杀掉子进程。
        save_pdf: 把引擎自己排好版的单语 PDF 另存到该路径（译文 PDF 直接用它）。
    """
    home = engine_home()
    if home is None:
        raise EngineError("BabelDOC 引擎未安装")
    launcher_cmd = launcher(home)
    if launcher_cmd is None:
        raise EngineError("BabelDOC 引擎启动文件缺失")
    if not str(cfg.get("api_key") or "").strip():
        raise EngineError("缺少模型 API Key")

    cache = cache_dir(home)
    cache.mkdir(parents=True, exist_ok=True)

    work_root = Path(tempfile.mkdtemp(prefix="paperead-babeldoc-"))
    out_dir = work_root / "out"
    work_dir = work_root / "work"
    out_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        # 便携 Python 不该读用户目录里的 site-packages，避免版本串味。
        "PYTHONNOUSERSITE": "1",
        "BABELDOC_CACHE_DIR": str(cache),
    }
    key = str(cfg.get("api_key") or "")
    command = _build_command(launcher_cmd, pdf, out_dir, work_dir, cfg, qps=qps)

    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    log.info("启动 BabelDOC：%s", " ".join(command[:1] + command[1:6]))
    started = time.time()
    tail: list[str] = []
    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
                cwd=str(work_root),
                creationflags=creationflags,
            )
        except OSError as exc:
            raise EngineError(f"无法启动 BabelDOC：{exc}") from exc

        assert proc.stdout is not None
        # 单独一个读取协程把 stdout 推进队列：主循环用 2 秒超时轮询取消标记，
        # 否则 rich 进度长时间不换行时「取消」要等整篇翻完才生效。
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()

        async def pump() -> None:
            assert proc.stdout is not None
            while True:
                chunk = await proc.stdout.readline()
                if not chunk:
                    break
                queue.put_nowait(chunk)
            queue.put_nowait(None)

        pump_task = asyncio.create_task(pump())
        try:
            while True:
                if should_cancel is not None and should_cancel():
                    proc.kill()
                    await proc.wait()
                    raise EngineCancelled("翻译任务已取消")
                if time.time() - started > timeout:
                    proc.kill()
                    await proc.wait()
                    raise EngineError("BabelDOC 运行超时")
                try:
                    raw = await asyncio.wait_for(queue.get(), timeout=2.0)
                except TimeoutError:
                    continue
                if raw is None:
                    break
                line = raw.decode("utf-8", errors="replace")
                if key:
                    line = line.replace(key, "***")
                plain = _ANSI_RE.sub("", line).strip()
                if plain:
                    tail.append(plain[-300:])
                    del tail[:-30]
                parsed = _parse_progress(line)
                if parsed and on_progress is not None:
                    on_progress(*parsed)
            code = await proc.wait()
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            raise
        finally:
            pump_task.cancel()
        if code != 0:
            detail = "\n".join(tail[-8:])[-800:]
            raise EngineError(f"BabelDOC 退出码 {code}：{detail}")

        il_files = sorted(
            work_dir.rglob("il_translated.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not il_files:
            raise EngineError("BabelDOC 没有产出 il_translated.json")
        try:
            il = json.loads(il_files[0].read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise EngineError(f"读取 BabelDOC 中间表示失败：{exc}") from exc
        if not il.get("page"):
            raise EngineError("BabelDOC 未识别到可翻译页面")
        # BabelDOC 自己排好版的 PDF 比我们回写原版面更稳（公式/表格/行距
        # 都由它处理），译文 PDF 直接用它；应用侧不再维护第二套回写实现。
        saved: dict[str, Path] = {}
        if save_pdf is not None:
            mono = await asyncio.to_thread(_save_pdf_copy, out_dir, Path(save_pdf), "mono")
            if mono is not None:
                saved["mono_pdf"] = mono
        if on_progress is not None:
            on_progress(1.0, "完成")
        stats: dict[str, Any] = {"elapsed_ms": int((time.time() - started) * 1000)}
        for name, path in saved.items():
            stats[name] = str(path)
        return il, stats
    finally:
        shutil.rmtree(work_root, ignore_errors=True)


__all__ = [
    "LANG_OUT",
    "EngineCancelled",
    "EngineError",
    "available",
    "cache_dir",
    "engine_home",
    "map_translations",
    "run",
    "status",
]
