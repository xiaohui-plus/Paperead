"""全局综述模板：用户上传的模板与「从文献库选一篇综述当模板」。

设置页选定的模板落在 ``data/review-templates/``：

    settings.json             # {"selected": "paper:p_xxx"} / upload:<文件名>
    uploads/<文件名>.md        # 用户上传的模板正文
    papers/<paperId>.md       # 从文献库 PDF 目录自动生成的骨架缓存

解析顺序由调用方保证：本次指定 > 项目设置 > 全局设置 > 项目内上传 > 系统默认。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from server.research.files import safe_name
from server.research.review_standard import (
    BUILTIN_TEMPLATES,
    sections_from_markdown,
    template_sections,
)

DIR_NAME = "review-templates"
SETTINGS_NAME = "settings.json"
UPLOADS_DIR = "uploads"
PAPERS_DIR = "papers"
MAX_TEMPLATE_BYTES = 2 * 1024 * 1024
ALLOWED_SUFFIXES = {".md", ".markdown", ".txt"}

_SKIP_SECTION = re.compile(
    r"^(references?|bibliography|acknowledg(e)?ments?|appendix|supplementary)\b",
    re.IGNORECASE,
)
_NUMBER_PREFIX = re.compile(
    r"^(?:\d+(?:\.\d+)*\.?|[IVXLC]+\.|[A-Z]\.)\s+"
)


class TemplateError(ValueError):
    """模板不可用（文件类型、内容或目标论文不存在）。"""


def templates_root(data_dir: Path) -> Path:
    return Path(data_dir) / DIR_NAME


def root_for(research_root: Path) -> Path:
    """``data/research`` → ``data/review-templates``。"""
    return templates_root(Path(research_root).parent)


def uploads_dir(root: Path) -> Path:
    path = Path(root) / UPLOADS_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def papers_dir(root: Path) -> Path:
    path = Path(root) / PAPERS_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


# ── 选择持久化 ──────────────────────────────────────────


def read_choice(root: Path) -> str:
    try:
        payload = json.loads((Path(root) / SETTINGS_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("selected") or "")


def write_choice(root: Path, value: str) -> None:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {}
    path = root / SETTINGS_NAME
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(existing, dict):
            payload = existing
    except (OSError, ValueError):
        pass
    value = (value or "").strip()
    if value:
        payload["selected"] = value
    else:
        payload.pop("selected", None)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


# ── 上传模板 ────────────────────────────────────────────


def _upload_paths(root: Path) -> list[Path]:
    folder = Path(root) / UPLOADS_DIR
    if not folder.is_dir():
        return []
    return sorted(
        (
            path
            for path in folder.iterdir()
            if path.is_file() and path.suffix.lower() in ALLOWED_SUFFIXES
        ),
        key=lambda path: path.name.lower(),
    )


def list_uploads(root: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for path in _upload_paths(root):
        sections = template_sections(path)
        items.append(
            {
                "id": f"upload:{path.name}",
                "name": path.stem,
                "source": "upload",
                "description": f"已上传的模板 · {len(sections)} 个章节",
                "sections": sections,
            }
        )
    return items


def save_upload(root: Path, filename: str, data: bytes) -> dict[str, Any]:
    suffix = Path(filename or "").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise TemplateError("只支持 Markdown / 文本模板（.md / .txt）")
    if not data:
        raise TemplateError("模板文件是空的")
    if len(data) > MAX_TEMPLATE_BYTES:
        raise TemplateError("模板文件不能超过 2 MB")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise TemplateError("模板需要是 UTF-8 编码的文本") from error
    if "##" not in text:
        raise TemplateError("模板里至少需要一个二级标题（## 章节名）")

    name = safe_name(filename, fallback="综述模板.md")
    target = uploads_dir(root) / name
    target.write_text(text, encoding="utf-8")
    sections = sections_from_markdown(text)
    return {
        "id": f"upload:{target.name}",
        "name": target.stem,
        "source": "upload",
        "description": f"已上传的模板 · {len(sections)} 个章节",
        "sections": sections,
    }


def delete_upload(root: Path, name: str) -> bool:
    filename = safe_name(name)
    target = Path(root) / UPLOADS_DIR / filename
    if not target.is_file():
        return False
    target.unlink()
    if read_choice(root) == f"upload:{filename}":
        write_choice(root, "default")
    return True


# ── 文献库论文 → 章节骨架 ───────────────────────────────


def _clean_title(title: str) -> str:
    text = _NUMBER_PREFIX.sub("", title.strip())
    return re.sub(r"\s+", " ", text).strip(" .")


def _usable(title: str) -> bool:
    cleaned = _clean_title(title)
    return (
        2 <= len(cleaned) <= 90
        and not _SKIP_SECTION.match(cleaned)
        and not re.fullmatch(r"[\d\s.]+", cleaned)
    )


def _sections_from_toc(doc: Any) -> list[str]:
    try:
        toc = doc.get_toc(simple=True)
    except Exception:
        return []
    level1: list[str] = []
    level2: list[str] = []
    for level, title, _page in toc:
        title = str(title or "")
        if not _usable(title):
            continue
        if level == 1:
            level1.append(_clean_title(title))
        elif level == 2:
            level2.append(_clean_title(title))
    chosen = level1 if len(level1) >= 3 else level1 + level2
    return _dedupe(chosen)


def _sections_from_fonts(doc: Any) -> list[str]:
    """没有书签时，按字号/加粗挑出像章节标题的行。"""
    sizes: Counter[float] = Counter()
    lines: list[tuple[str, float, bool]] = []
    for page in doc:
        try:
            payload = page.get_text("dict")
        except Exception:
            continue
        for block in payload.get("blocks", []):
            for line in block.get("lines", []):
                spans = line.get("spans") or []
                text = "".join(str(span.get("text") or "") for span in spans).strip()
                if not text:
                    continue
                size = max(float(span.get("size") or 0) for span in spans)
                bold = any(int(span.get("flags") or 0) & 16 for span in spans)
                sizes[round(size, 1)] += len(text)
                lines.append((text, size, bold))
    if not lines:
        return []
    body = sizes.most_common(1)[0][0]
    found: list[str] = []
    for text, size, bold in lines:
        if not _usable(text) or text.endswith((".", "。", ",", "，", "；", ";")):
            continue
        if re.fullmatch(r"[\d\s.\-]+", text):
            continue
        if size >= body * 1.08 or (bold and size >= body * 0.98):
            found.append(_clean_title(text))
    return _dedupe(found)[:25]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        key = item.lower()
        if item and key not in seen:
            seen.add(key)
            output.append(item)
    return output


def extract_pdf_sections(pdf_path: Path) -> list[str]:
    try:
        import pymupdf
    except Exception:
        return []
    try:
        doc = pymupdf.open(pdf_path)
    except Exception:
        return []
    try:
        sections = _sections_from_toc(doc)
        if len(sections) < 3:
            sections = _sections_from_fonts(doc)
        return sections
    finally:
        doc.close()


def _markdown(title: str, sections: list[str]) -> str:
    lines = [f"# {title}".strip(), ""]
    for section in sections:
        lines.extend([f"## {section}", ""])
    return "\n".join(lines)


def paper_template(
    root: Path,
    library: Any | None,
    paper_id: str,
    *,
    refresh: bool = False,
) -> tuple[list[str], str, str]:
    """返回 (章节, 名称, 错误)。结果缓存在 ``papers/<id>.md``。"""
    if library is None:
        return [], paper_id, "文献库不可用"
    row = library.get(paper_id)
    if row is None:
        return [], paper_id, "文献库里没有这篇论文"

    cache = papers_dir(root) / f"{safe_name(paper_id, fallback='paper')}.md"
    title = str(row.get("title") or paper_id)
    if cache.is_file() and not refresh:
        sections = template_sections(cache)
        if sections:
            return sections, title, ""

    pdf_path = library.pdf_path(paper_id)
    if not pdf_path or not Path(pdf_path).is_file():
        return [], title, "这篇论文还没有本地 PDF，无法提取章节"
    sections = extract_pdf_sections(Path(pdf_path))
    if len(sections) < 3:
        return [], title, "没有从 PDF 里识别出足够章节，写作时可由智能体读原文生成"
    cache.write_text(_markdown(title, sections), encoding="utf-8")
    return sections, title, ""


# ── 文献库论文 → 写作指南（结构 + 图表 + 语言） ──────────

_FIGURE_RE = re.compile(
    r"^\s*(figure|fig\.?)\s*([0-9]+[a-z]?)\s*([.:：])?\s*(.+)$", re.IGNORECASE
)
_TABLE_RE = re.compile(
    r"^\s*(table)\s*([0-9]+[a-z]?)\s*([.:：])?\s*(.+)$", re.IGNORECASE
)
_REFERENCE_HEADING = re.compile(
    r"^\s*(references?|bibliography)\s*$", re.IGNORECASE
)
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")

# 模板语言风格里统计的连接词与模糊限定词。只做频次参考，不做硬性约束。
_CONNECTIVES = (
    "however", "moreover", "furthermore", "in addition", "therefore",
    "thus", "consequently", "in contrast", "by contrast", "on the other hand",
    "meanwhile", "specifically", "notably", "overall", "in summary",
    "although", "whereas", "while", "similarly", "instead",
)
_HEDGES = (
    "may", "might", "could", "suggest", "indicate", "likely",
    "potentially", "appear", "seem", "approximately", "to some extent",
)

# 图表类型与用途：从标题和正文引用推断，供写作时决定“这一章该放什么图表、为什么放”。
_FIGURE_TYPES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("timeline", "history", "evolution"), "时间线图", "交代领域或方法的时间演进，适合放在引言"),
    (("architecture", "framework", "overview", "pipeline", "workflow"), "框架/架构示意图", "说明方法整体结构与数据流，适合放在方法章"),
    (("taxonomy", "classification", "categor"), "分类图", "给出任务或方法分类，适合放在引言或方法开头"),
    (("comparison", "performance", "results", "benchmark", "accuracy", "evaluation"), "性能对比图", "横向比较方法指标，适合放在结果或讨论"),
    (("heatmap", "attention", "saliency", "importance"), "热图/可视化", "展示注意力、误差或重要性分布，适合放在结果分析"),
    (("molecule", "protein", "pocket", "pose", "conformation", "structure", "3d"), "结构示意图", "展示分子、口袋或结合模式，适合放在结构相关章节"),
    (("distribution", "histogram", "boxplot"), "分布图", "展示数据或误差分布，适合放在结果分析"),
)
_TABLE_TYPES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("dataset", "statistics", "summary", "overview"), "数据集/统计汇总表", "汇总数据规模、任务与划分，适合放在方法或实验设置"),
    (("comparison", "performance", "results", "benchmark", "evaluation"), "方法对比表", "对比方法、输入、指标与结论，适合放在对应任务章"),
    (("ablation",), "消融表", "说明各组件或模块的贡献，适合放在结果或讨论"),
    (("notation", "symbol", "definition"), "符号/定义表", "统一术语与符号，适合放在方法开头"),
)


def _classify(kind: str, text: str) -> tuple[str, str]:
    """按关键词判断图表类型与用途；判断不出时给出保守的兜底说明。"""
    lowered = text.lower()
    rules = _TABLE_TYPES if kind == "table" else _FIGURE_TYPES
    for keywords, chart_type, purpose in rules:
        if any(keyword in lowered for keyword in keywords):
            return chart_type, purpose
    if kind == "table":
        return "汇总表（类型未明）", "按模板位置汇总可比信息；没有可比数值时说明不可比原因"
    return "示意图（类型未明）", "按模板位置补充说明性图示；没有数据支撑时改用表或文字"


def _sections_with_pages(doc: Any) -> list[dict[str, Any]]:
    """章节 + 起始页，用于把图表归到章节。没有书签时按字号兜底。"""
    try:
        toc = doc.get_toc(simple=True)
    except Exception:
        toc = []
    spans: list[dict[str, Any]] = []
    for level, title, page in toc:
        title = str(title or "")
        if level != 1 or not _usable(title):
            continue
        try:
            start = max(1, int(page))
        except (TypeError, ValueError):
            continue
        spans.append({"title": _clean_title(title), "page": start})
    if len(spans) < 3:
        spans = _font_sections_with_pages(doc)
    spans.sort(key=lambda item: item["page"])
    total = int(getattr(doc, "page_count", 0) or 0)
    for index, span in enumerate(spans):
        end = spans[index + 1]["page"] - 1 if index + 1 < len(spans) else total
        span["endPage"] = max(span["page"], min(end, total))
    return spans


def _font_sections_with_pages(doc: Any) -> list[dict[str, Any]]:
    sizes: Counter[float] = Counter()
    lines: list[tuple[str, float, bool, int]] = []
    for page_number, page in enumerate(doc, 1):
        try:
            payload = page.get_text("dict")
        except Exception:
            continue
        for block in payload.get("blocks", []):
            for line in block.get("lines", []):
                spans = line.get("spans") or []
                text = "".join(str(span.get("text") or "") for span in spans).strip()
                if not text:
                    continue
                size = max(float(span.get("size") or 0) for span in spans)
                bold = any(int(span.get("flags") or 0) & 16 for span in spans)
                sizes[round(size, 1)] += len(text)
                lines.append((text, size, bold, page_number))
    if not lines:
        return []
    body = sizes.most_common(1)[0][0]
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for text, size, bold, page_number in lines:
        if not _usable(text) or text.endswith((".", "。", ",", "，", "；", ";")):
            continue
        if re.fullmatch(r"[\d\s.\-]+", text):
            continue
        if size >= body * 1.08 or (bold and size >= body * 0.98):
            title = _clean_title(text)
            key = title.lower()
            if title and key not in seen:
                seen.add(key)
                found.append({"title": title, "page": page_number})
    return found[:25]


def _page_of_span(spans: list[dict[str, Any]], page_number: int) -> str:
    for span in spans:
        if span["page"] <= page_number <= span.get("endPage", span["page"]):
            return str(span["title"])
    return ""


def _reference_start(doc: Any) -> int | None:
    total = int(getattr(doc, "page_count", 0) or 0)
    for index in range(total):
        if index < total * 0.4:
            continue
        try:
            lines = doc[index].get_text().splitlines()
        except Exception:
            continue
        for line in lines[:12]:
            if _REFERENCE_HEADING.match(line.strip()):
                return index
    return None


def _extract_captions(
    doc: Any, spans: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """抓 Figure / Table 标题，标注所属章节、类型、用途与正文引用依据。"""
    reference_start = _reference_start(doc)
    figures: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    seen: set[str] = set()
    total = int(getattr(doc, "page_count", 0) or 0)
    for index in range(total):
        if reference_start is not None and index >= reference_start:
            break
        try:
            page_text = doc[index].get_text()
            lines = page_text.splitlines()
        except Exception:
            continue
        sentences = [
            re.sub(r"\s+", " ", item).strip()
            for item in re.split(r"(?<=[.!?])\s+", page_text)
            if len(item.strip()) >= 20
        ]
        for line in lines:
            text = line.strip()
            for regex, kind, bucket in (
                (_FIGURE_RE, "figure", figures),
                (_TABLE_RE, "table", tables),
            ):
                match = regex.match(text)
                if not match:
                    continue
                number = match.group(2)
                separator = match.group(3) or ""
                caption = re.sub(r"\s+", " ", match.group(4)).strip()
                key = f"{kind}:{number.lower()}"
                if len(caption) < 6 or key in seen:
                    continue
                seen.add(key)
                head = "figure|fig\\.?" if kind == "figure" else "table"
                token = re.compile(rf"\b(?:{head})\s*{re.escape(number)}\b", re.IGNORECASE)
                context = ""
                for sentence in sentences:
                    if token.search(sentence) and not sentence.lower().startswith(
                        ("figure", "table")
                    ):
                        context = sentence[:240]
                        break
                chart_type, purpose = _classify(kind, f"{caption} {context}")
                bucket.append(
                    {
                        "label": f"{'Figure' if kind == 'figure' else 'Table'} {number}",
                        "caption": caption[:220],
                        "type": chart_type,
                        "purpose": purpose,
                        "context": context,
                        "separator": separator,
                        "page": index + 1,
                        "section": _page_of_span(spans, index + 1),
                    }
                )
    return figures[:40], tables[:40]


def _style_profile(
    text: str, paragraph_lengths: list[int] | None = None
) -> dict[str, Any]:
    """从模板正文估计句长、引用方式与常用连接词，供写作时模仿。"""
    body = text.strip()
    if not body:
        return {}
    words = re.findall(r"[A-Za-z][A-Za-z\-']*", body)
    word_count = len(words)
    cjk_count = len(_CJK_RE.findall(body))
    sentences = [s.strip() for s in re.split(r"[.!?。！？]+", body) if len(s.strip()) >= 8]
    if cjk_count > 200 and cjk_count > word_count * 2:
        lengths = [len(_CJK_RE.findall(s)) for s in sentences]
        avg_length = round(sum(lengths) / len(lengths), 1) if lengths else 0
        long_ratio = round(sum(1 for n in lengths if n > 60) / len(lengths), 2) if lengths else 0
        length_unit = "汉字"
    else:
        lengths = [len(re.findall(r"[A-Za-z][A-Za-z\-']*", s)) for s in sentences]
        lengths = [n for n in lengths if n > 0]
        avg_length = round(sum(lengths) / len(lengths), 1) if lengths else 0
        long_ratio = round(sum(1 for n in lengths if n > 30) / len(lengths), 2) if lengths else 0
        length_unit = "词"

    numeric = len(re.findall(r"\[\d+(?:\s*[-–,]\s*\d+)*\]", body))
    author_year = len(
        re.findall(
            r"\([A-Z][A-Za-z\-]+(?:\s+et al\.)?,?\s+(?:19|20)\d{2}\)", body
        )
    )
    if numeric >= 5 and numeric >= author_year * 2:
        citation_style = "numeric"
    elif author_year >= 5 and author_year >= numeric * 2:
        citation_style = "author-year"
    elif numeric or author_year:
        citation_style = "mixed"
    else:
        citation_style = "unknown"

    def ranked(terms: tuple[str, ...]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        base = word_count or max(len(body), 1)
        for term in terms:
            count = len(re.findall(rf"\b{re.escape(term)}\b", body, re.IGNORECASE))
            if count < 3:
                continue
            rows.append({"term": term, "count": count, "per10k": round(count / base * 10000, 1)})
        rows.sort(key=lambda item: item["count"], reverse=True)
        return rows[:6]

    first_person = len(re.findall(r"\b(we|our|us)\b", body, re.IGNORECASE))
    if paragraph_lengths:
        ordered = sorted(n for n in paragraph_lengths if n >= 80)
        paragraph_length = ordered[len(ordered) // 2] if ordered else 0
    else:
        paragraphs = [p for p in re.split(r"\n\s*\n", body) if len(p.strip()) >= 80]
        values = [
            len(_CJK_RE.findall(p)) if cjk_count > 200 else len(p) for p in paragraphs
        ]
        values.sort()
        paragraph_length = values[len(values) // 2] if values else 0

    return {
        "citationStyle": citation_style,
        "sentenceUnit": length_unit,
        "avgSentenceLength": avg_length,
        "longSentenceRatio": long_ratio,
        "avgParagraphLength": paragraph_length,
        "firstPersonPer10k": round(first_person / (word_count or 1) * 10000, 1),
        "connectives": ranked(_CONNECTIVES),
        "hedges": ranked(_HEDGES),
    }


def _heading_numbering(doc: Any) -> tuple[str, str]:
    """判断一级标题的编号体系：罗马、阿拉伯、字母、中文或不编号。"""
    try:
        toc = doc.get_toc(simple=True)
    except Exception:
        toc = []
    titles = [str(title) for level, title, _page in toc if level == 1]
    patterns = {
        "roman": r"^[IVXLC]+[.)]\s+",
        "arabic": r"^\d+(?:\.\d+)*[.)]?\s+",
        "letter": r"^[A-Z][.)]\s+",
        "chinese": r"^[一二三四五六七八九十]+[、.]\s*",
    }
    def pick(items: list[str]) -> tuple[str, int]:
        counts = {
            key: sum(1 for title in items if re.match(pattern, title.strip()))
            for key, pattern in patterns.items()
        }
        priority = ["roman", "arabic", "letter", "chinese"]
        best = max(priority, key=lambda key: (counts[key], -priority.index(key)))
        return best, counts[best]

    best, count = pick(titles) if len(titles) >= 3 else ("unknown", 0)
    if count >= 2:
        return best, titles[0].strip()

    # 目录里常去掉编号：以目录标题为锚点，回正文前几页找带编号的同一行。
    candidates: list[str] = []
    page_count = int(getattr(doc, "page_count", 0) or 0)
    for title in titles[:12]:
        needle = re.sub(r"\s+", " ", title.strip().lower())
        if not needle:
            continue
        found = ""
        for index in range(min(8, page_count)):
            try:
                lines = doc[index].get_text().splitlines()
            except Exception:
                continue
            for line in lines:
                text = re.sub(r"\s+", " ", line.strip())
                if needle in text.lower() and len(text) <= len(title) + 16:
                    found = text
                    break
            if found:
                break
        if found:
            candidates.append(found)
    if candidates:
        best, count = pick(candidates)
        if count >= 2:
            return best, candidates[0]
    if titles:
        return "none", titles[0].strip()
    return "unknown", ""


def _format_profile(
    doc: Any, figures: list[dict[str, Any]], tables: list[dict[str, Any]], style: dict[str, Any]
) -> dict[str, Any]:
    """提取页面、栏数、字号、页边距、标题编号与图表题格式。"""
    rect = doc[0].rect
    width_mm = rect.width * 25.4 / 72
    height_mm = rect.height * 25.4 / 72
    if abs(width_mm - 210) <= 8 and abs(height_mm - 297) <= 12:
        page_size = "A4"
    elif abs(width_mm - 216) <= 8 and abs(height_mm - 279) <= 12:
        page_size = "Letter"
    else:
        page_size = f"{round(width_mm)}×{round(height_mm)} mm"

    size_weights: Counter[float] = Counter()
    title_size = 0.0
    blocks: list[tuple[float, float, float, float]] = []
    for index in range(min(int(getattr(doc, "page_count", 0) or 0), 8)):
        try:
            payload = doc[index].get_text("dict")
        except Exception:
            continue
        for block in payload.get("blocks", []):
            if block.get("type") != 0:
                continue
            x0, y0, x1, y1 = block.get("bbox") or (0, 0, 0, 0)
            text = "".join(
                str(span.get("text") or "")
                for line in block.get("lines", [])
                for span in line.get("spans", [])
            ).strip()
            if len(text) < 40:
                continue
            blocks.append((x0, y0, x1, y1))
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    content = str(span.get("text") or "").strip()
                    if not content:
                        continue
                    size = round(float(span.get("size") or 0), 1)
                    size_weights[size] += len(content)
                    if index == 0 and len(content) >= 8:
                        title_size = max(title_size, size)
    body_size = size_weights.most_common(1)[0][0] if size_weights else 0.0

    mid = rect.width / 2
    left_blocks = [box for box in blocks if box[2] < mid + 20 and box[0] < mid - 40]
    right_blocks = [box for box in blocks if box[0] > mid - 20]
    columns = 2 if len(left_blocks) >= 8 and len(right_blocks) >= 8 else 1

    margins = {}
    if blocks:
        left = min(box[0] for box in blocks)
        top = min(box[1] for box in blocks)
        right = rect.width - max(box[2] for box in blocks)
        bottom = rect.height - max(box[3] for box in blocks)
        convert = 25.4 / 72
        margins = {
            "left": round(left * convert),
            "right": round(right * convert),
            "top": round(top * convert),
            "bottom": round(bottom * convert),
        }

    heading_style, heading_example = _heading_numbering(doc)
    separators = Counter(
        str(row.get("separator") or "") for row in (figures + tables)
    )
    separator = separators.most_common(1)[0][0] if separators else ""

    first_pages = ""
    for index in range(min(2, int(getattr(doc, "page_count", 0) or 0))):
        try:
            first_pages += "\n" + doc[index].get_text()
        except Exception:
            continue
    lowered = first_pages.lower()
    has_abstract = bool(re.search(r"\babstract\b|摘\s*要", lowered))
    has_keywords = bool(re.search(r"\bkeywords?\b|关\s*键\s*词", lowered))

    reference_heading = ""
    reference_start = _reference_start(doc)
    if reference_start is not None:
        try:
            for line in doc[reference_start].get_text().splitlines()[:12]:
                if _REFERENCE_HEADING.match(line.strip()):
                    reference_heading = line.strip()
                    break
        except Exception:
            pass

    return {
        "pageSize": page_size,
        "columns": columns,
        "bodyFontSize": body_size,
        "titleFontSize": title_size,
        "marginsMm": margins,
        "headingStyle": heading_style,
        "headingExample": heading_example,
        "captionSeparator": separator,
        "abstract": has_abstract,
        "keywords": has_keywords,
        "referenceHeading": reference_heading,
        "citationStyle": style.get("citationStyle") or "unknown",
    }


def _format_notes(fmt: dict[str, Any]) -> list[str]:
    notes: list[str] = []
    if fmt.get("columns") == 2:
        notes.append("模板为双栏排版；Markdown 不模拟分栏，LaTeX 导出用 twocolumn 或期刊模板。")
    margins = fmt.get("marginsMm") or {}
    if fmt.get("pageSize") or margins:
        edges = "、".join(
            f"{key} {value}mm"
            for key, value in margins.items()
            if key in {"left", "right", "top", "bottom"}
        )
        notes.append(
            f"页面 {fmt.get('pageSize') or '未识别'}，正文约 {fmt.get('bodyFontSize') or 0}pt"
            + (f"，页边距约 {edges}" if edges else "")
            + "；LaTeX 用 "
            + ("letterpaper" if fmt.get("pageSize") == "Letter" else "a4paper")
            + " 与 geometry 对齐。"
        )
    heading = fmt.get("headingStyle")
    mapping = {
        "roman": "一级标题用罗马数字（I. II. ...），二级可用字母或阿拉伯数字；Markdown 用 ## 对应一级。",
        "arabic": "一级标题用阿拉伯数字（1. 2.），二级用 1.1；Markdown 用 ## / ### 对应层级。",
        "letter": "一级标题用大写字母（A. B. ...）；Markdown 用 ## 对应一级。",
        "chinese": "一级标题用中文编号（一、二、）；Markdown 用 ## 对应一级。",
        "none": "模板标题不带编号；本稿可保留简洁标题，不加数字前缀。",
    }
    if heading in mapping:
        notes.append(mapping[heading])
    separator = fmt.get("captionSeparator")
    if separator == ":":
        notes.append("图表题在编号后用冒号，如「Figure 1: ...」「图 1：...」。")
    elif separator == ".":
        notes.append("图表题在编号后用句点，如「Figure 1. ...」。")
    elif separator == "":
        notes.append("图表题与编号之间不用分隔符。")
    if fmt.get("abstract"):
        notes.append("模板含摘要；本稿保留摘要段，写清目的、方法、结果与结论。")
    if fmt.get("keywords"):
        notes.append("模板含关键词；本稿补 3–5 个关键词。")
    if fmt.get("referenceHeading"):
        notes.append(f"参考文献节标题用「{fmt['referenceHeading']}」，引用格式按 style.citationStyle。")
    return notes


def _style_notes(style: dict[str, Any]) -> list[str]:
    notes: list[str] = []
    mapping = {
        "numeric": "模板采用数字编号引用（如 [12]）；写作时用 [n] 并在文末列一一对应的文献表。",
        "author-year": "模板采用作者-年份引用；写作时在正文标注（作者 年份），文末按作者排序。",
        "mixed": "模板混用数字与作者-年份引用；建议统一为数字编号，避免两种格式并存。",
    }
    if style.get("citationStyle") in mapping:
        notes.append(mapping[style["citationStyle"]])
    unit = style.get("sentenceUnit") or "词"
    average = style.get("avgSentenceLength") or 0
    if average:
        notes.append(f"模板平均句长约 {average} {unit}，写作时保持长短句交替，避免连续长句。")
    if style.get("longSentenceRatio", 0) >= 0.25:
        notes.append("模板长句比例偏高；模仿时每段至少留 2 个短句，降低阅读负担。")
    connectives = [row["term"] for row in style.get("connectives") or []][:4]
    if connectives:
        notes.append("常用衔接：" + "、".join(connectives) + "；转折与递进优先用这类词，不要堆“首先/其次/最后”。")
    hedges = [row["term"] for row in style.get("hedges") or []][:3]
    if hedges:
        notes.append("常用限定表达：" + "、".join(hedges) + "；结论留有余地，但不弱化已确认的事实。")
    return notes


def _guide_markdown(profile: dict[str, Any]) -> str:
    """把指南落成 Markdown，方便用户与智能体直接阅读。"""
    title = profile.get("title") or profile.get("paperId") or "模板"
    lines = [f"# 模板写作指南：{title}", ""]
    stats = profile.get("sectionStats") or []
    if stats:
        lines.extend(["## 结构与篇幅", "", "| 章节 | 起始页 | 页数 | 图 | 表 | 位图 | 正文字符 |", "| --- | --- | --- | --- | --- | --- | --- |"])
        for row in stats:
            lines.append(
                f"| {row['title']} | {row['page']} | {row.get('pages', '')} | "
                f"{row.get('figures', 0)} | {row.get('tables', 0)} | "
                f"{row.get('images', 0)} | {row.get('chars', 0)} |"
            )
        lines.append("")
    for key, heading in (("figures", "图"), ("tables", "表")):
        rows = profile.get(key) or []
        if not rows:
            continue
        lines.extend(
            [
                f"## {heading}的放置",
                "",
                "| 编号 | 类型 | 标题 | 所属章节 | 页码 |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for row in rows:
            caption = str(row.get("caption") or "").replace("|", "\\|")
            lines.append(
                f"| {row.get('label', '')} | {row.get('type', '')} | {caption} | "
                f"{row.get('section') or '未识别'} | {row.get('page', '')} |"
            )
        lines.extend(["", f"{heading}的制作依据（类型与用途由标题和正文引用推断）：", ""])
        for row in rows[:12]:
            line = f"- {row.get('label', '')}（{row.get('type', '')}）：{row.get('purpose', '')}"
            context = str(row.get("context") or "").strip()
            if context:
                line += f"；正文引用：“{context}”"
            lines.append(line)
        lines.append("")
    style = profile.get("style") or {}
    if style:
        lines.extend(["## 语言与格式要求", ""])
        for note in profile.get("notes") or []:
            lines.append(f"- {note}")
        lines.append("")
    fmt = profile.get("format") or {}
    if fmt:
        heading_labels = {
            "roman": "罗马数字（I. II.）",
            "arabic": "阿拉伯数字（1. 2.）",
            "letter": "大写字母（A. B.）",
            "chinese": "中文编号（一、二、）",
            "none": "不编号",
            "unknown": "未识别",
        }
        columns = "双栏" if fmt.get("columns") == 2 else "单栏"
        margins = fmt.get("marginsMm") or {}
        edge_text = "、".join(
            f"{key} {value}mm"
            for key, value in margins.items()
            if key in {"left", "right", "top", "bottom"}
        )
        separator = fmt.get("captionSeparator")
        caption_text = (
            "编号后用冒号（Figure 1: ...）"
            if separator == ":"
            else "编号后用句点（Figure 1. ...）"
            if separator == "."
            else "编号后不加分隔符"
            if separator == ""
            else "未识别"
        )
        rows = [
            ("页面", f"{fmt.get('pageSize') or '未识别'} · {columns} · 正文约 {fmt.get('bodyFontSize') or 0}pt"),
            ("页边距", edge_text or "未识别"),
            (
                "标题编号",
                heading_labels.get(str(fmt.get("headingStyle")), "未识别")
                + (f"（例：{fmt['headingExample']}）" if fmt.get("headingExample") else ""),
            ),
            ("图表题", caption_text),
            (
                "摘要 / 关键词",
                ("有" if fmt.get("abstract") else "无") + " / " + ("有" if fmt.get("keywords") else "无"),
            ),
            (
                "参考文献",
                (fmt.get("referenceHeading") or "未识别")
                + f"（{fmt.get('citationStyle')}）",
            ),
        ]
        lines.extend(["## 论文格式", "", "| 项目 | 模板 |", "| --- | --- |"])
        for label, value in rows:
            lines.append(f"| {label} | {str(value).replace('|', '\\|')} |")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _image_counts(
    doc: Any, spans: list[dict[str, Any]], limit: int
) -> tuple[int, dict[str, int]]:
    """统计正文里的位图数量，作为图题缺失时的兜底信号。"""
    total = 0
    per_section: dict[str, int] = {}
    for index in range(min(limit, int(getattr(doc, "page_count", 0) or 0))):
        try:
            count = len(doc[index].get_images(full=True))
        except Exception:
            count = 0
        if not count:
            continue
        total += count
        section = _page_of_span(spans, index + 1)
        per_section[section] = per_section.get(section, 0) + count
    return total, per_section


def extract_pdf_profile(pdf_path: Path) -> dict[str, Any]:
    """从 PDF 提取章节、图表放置与语言风格；失败返回空 dict。"""
    try:
        import pymupdf
    except Exception:
        return {}
    try:
        doc = pymupdf.open(pdf_path)
    except Exception:
        return {}
    try:
        spans = _sections_with_pages(doc)
        sections = [span["title"] for span in spans] or extract_pdf_sections(pdf_path)
        figures, tables = _extract_captions(doc, spans)
        reference_start = _reference_start(doc) or int(getattr(doc, "page_count", 0) or 0)
        page_texts: list[str] = []
        paragraph_lengths: list[int] = []
        for index in range(reference_start):
            try:
                page = doc[index]
                page_texts.append(page.get_text())
                blocks = page.get_text("dict").get("blocks", [])
            except Exception:
                page_texts.append("")
                continue
            for block in blocks:
                if block.get("type") != 0:
                    continue
                text = "".join(
                    str(span.get("text") or "")
                    for line in block.get("lines", [])
                    for span in line.get("spans", [])
                ).strip()
                if len(text) >= 80:
                    paragraph_lengths.append(len(text))
        image_total, image_by_section = _image_counts(doc, spans, reference_start)
        stats: list[dict[str, Any]] = []
        for span in spans:
            start = max(1, int(span["page"]))
            end = min(int(span.get("endPage") or start), len(page_texts))
            chars = sum(len(page_texts[p - 1]) for p in range(start, end + 1))
            stats.append(
                {
                    "title": span["title"],
                    "page": start,
                    "pages": max(0, end - start + 1),
                    "chars": chars,
                    "figures": sum(1 for row in figures if row.get("section") == span["title"]),
                    "tables": sum(1 for row in tables if row.get("section") == span["title"]),
                    "images": image_by_section.get(span["title"], 0),
                }
            )
        style = _style_profile("\n".join(page_texts), paragraph_lengths)
        format_profile = _format_profile(doc, figures, tables, style)
        notes = _style_notes(style) + _format_notes(format_profile)
        if not figures and not tables and image_total:
            dense = sorted(
                (row for row in stats if row.get("images")),
                key=lambda row: row["images"],
                reverse=True,
            )[:3]
            where = "、".join(f"{row['title']}（{row['images']} 张）" for row in dense)
            notes.append(
                f"文本层未取到图题（图可能是位图）；模板含 {image_total} 张位图"
                + (f"，主要分布在 {where}" if where else "")
                + "。写作时可据同章密度安排示意图或表。"
            )
        profile: dict[str, Any] = {
            "title": "",
            "sections": sections,
            "figures": figures,
            "tables": tables,
            "imageCount": image_total,
            "sectionStats": stats,
            "style": style,
            "format": format_profile,
            "notes": notes,
        }
        return profile
    finally:
        doc.close()


def paper_guide(
    root: Path,
    library: Any | None,
    paper_id: str,
    *,
    refresh: bool = False,
) -> dict[str, Any]:
    """文献库论文的写作指南（结构 + 图表 + 语言），带 JSON 缓存。"""
    if library is None:
        return {}
    row = library.get(paper_id)
    if row is None:
        return {}
    pdf_path = library.pdf_path(paper_id)
    if not pdf_path or not Path(pdf_path).is_file():
        return {}
    pdf = Path(pdf_path)
    cache_json = papers_dir(root) / f"{safe_name(paper_id, fallback='paper')}.guide.json"
    if cache_json.is_file() and not refresh:
        try:
            cached = json.loads(cache_json.read_text(encoding="utf-8"))
            stat = pdf.stat()
            if (
                isinstance(cached, dict)
                and cached.get("pdfSize") == stat.st_size
                and cached.get("pdfMtime") == int(stat.st_mtime)
            ):
                return cached
        except (OSError, ValueError):
            pass
    profile = extract_pdf_profile(pdf)
    if not profile:
        return {}
    profile["paperId"] = paper_id
    profile["title"] = str(row.get("title") or paper_id)
    profile["pdfSize"] = pdf.stat().st_size
    profile["pdfMtime"] = int(pdf.stat().st_mtime)
    profile["markdown"] = _guide_markdown(profile)
    try:
        cache_json.write_text(json.dumps(profile, ensure_ascii=False, indent=1), encoding="utf-8")
        if profile.get("sections"):
            cache_md = papers_dir(root) / f"{safe_name(paper_id, fallback='paper')}.md"
            if not cache_md.is_file() or refresh:
                cache_md.write_text(
                    _markdown(profile["title"], profile["sections"]), encoding="utf-8"
                )
    except OSError:
        pass
    return profile


# ── 列表与解析 ──────────────────────────────────────────


def list_options(root: Path) -> list[dict[str, Any]]:
    """系统默认 + 内置模板 + 用户上传，供设置页与 agent 查看。"""
    items: list[dict[str, Any]] = [
        {
            "id": "default",
            "name": "系统默认（五段式）",
            "source": "default",
            "description": "摘要 / 引言 / 方法 / 讨论 / 结论 + 3 个主题小节",
            "sections": [],
        }
    ]
    for template_id, meta in BUILTIN_TEMPLATES.items():
        items.append(
            {
                "id": template_id,
                "name": meta["name"],
                "source": "builtin",
                "description": meta["description"],
                "sections": sections_from_markdown(meta["markdown"]),
            }
        )
    items.extend(list_uploads(root))
    return items


def resolve_global(
    choice: str,
    root: Path,
    library: Any | None,
) -> tuple[list[str], str, str]:
    """解析全局设置里的 ``upload:`` / ``paper:`` 选择。"""
    value = (choice or "").strip()
    if value.startswith("upload:"):
        name = safe_name(value[7:])
        path = Path(root) / UPLOADS_DIR / name
        if not path.is_file():
            return [], value, f"没有找到模板：{name}"
        return template_sections(path), path.stem, ""
    if value.startswith("paper:"):
        return paper_template(Path(root), library, value[6:])
    return [], value, f"没有找到模板：{value}"


def describe(
    choice: str,
    root: Path,
    library: Any | None,
) -> dict[str, Any]:
    """给设置页返回当前选择的名称、章节与正文预览。"""
    value = (choice or "default").strip() or "default"
    if value in {"default", "auto"}:
        return {
            "id": "default",
            "name": "系统默认（五段式）",
            "source": "default",
            "sections": [],
            "markdown": "",
            "guide": {},
            "error": "",
        }
    if value in BUILTIN_TEMPLATES:
        meta = BUILTIN_TEMPLATES[value]
        return {
            "id": value,
            "name": meta["name"],
            "source": "builtin",
            "sections": sections_from_markdown(meta["markdown"]),
            "markdown": meta["markdown"],
            "guide": {},
            "error": "",
        }
    sections, label, error = resolve_global(value, Path(root), library)
    markdown = ""
    guide: dict[str, Any] = {}
    if value.startswith("upload:"):
        path = Path(root) / UPLOADS_DIR / safe_name(value[7:])
        if path.is_file():
            markdown = path.read_text(encoding="utf-8", errors="replace")
    elif value.startswith("paper:"):
        paper_id = value[6:]
        cache = papers_dir(root) / f"{safe_name(paper_id, fallback='paper')}.md"
        if cache.is_file():
            markdown = cache.read_text(encoding="utf-8", errors="replace")
        guide = paper_guide(Path(root), library, paper_id)
    source = "upload" if value.startswith("upload:") else "paper"
    return {
        "id": value,
        "name": label,
        "source": source,
        "sections": sections,
        "markdown": markdown,
        "guide": guide,
        "error": error,
    }
