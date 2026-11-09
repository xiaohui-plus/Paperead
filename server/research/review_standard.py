"""综述质量基线：把草稿和“目标综述”的差距变成 agent 可执行的补料清单。

引用审计只回答“引用是否可核对”，这里再回答“离一篇标准综述还差什么”：

- 骨架：摘要 / 引言 / 方法 / 讨论 / 结论 + 至少 3 个主题小节；
- 证据：本地全文来源 >= 5 篇、每个主题小节 >= 2 篇、页码级引用率 >= 60%；
- 引用：无未解析 paperId、无无引用论断；
- 规模：总引用 >= 15 处。

项目里放了 ``review-template.md``（或 ``综述模板.md``）时，用它的二级标题作为
必需骨架，覆盖默认 5 段式，方便按用户自己的模板（如 5A 模板）检查。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

TEMPLATE_NAMES = ("review-template.md", "综述模板.md", "综述模板.MD")

# (id, 展示名, 匹配关键词)。关键词按“标题包含”匹配，忽略大小写与编号前缀。
REQUIRED_SECTIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("abstract", "摘要", ("摘要", "abstract")),
    ("introduction", "引言/背景", ("引言", "背景", "introduction")),
    ("methods", "方法/检索策略", ("方法", "检索策略", "纳入与排除", "methods", "search strategy")),
    ("discussion", "讨论/局限", ("讨论", "局限", "争议", "discussion", "limitation")),
    ("conclusion", "结论/展望", ("结论", "展望", "conclusion", "future direction")),
)

MIN_TOPIC_SECTIONS = 3
MIN_SECTION_SOURCES = 2
MIN_FULLTEXT_SOURCES = 5
MIN_CITATIONS = 15
MIN_PAGE_RATIO = 0.6

_HEADING = re.compile(r"^(#{1,4})\s+(.+?)\s*$")
_NUMBER_PREFIX = re.compile(r"^[（(]?[0-9一二三四五六七八九十]+[、.．)）]\s*")
_MARKUP = re.compile(r"[*_`：:，,。.、\s]+")

# 系统内置的固定模板。用户在项目里选定后，会落成 files/review-template.md，
# 之后的引用审计与写作都按它检查；不选则用 DEFAULT 五段式基线。
BUILTIN_TEMPLATES: dict[str, dict[str, Any]] = {
    "narrative": {
        "name": "叙述性综述",
        "description": "按机制—证据—争议—展望展开，适合临床与综述类选题。",
        "markdown": (
            "# 叙述性综述模板\n\n"
            "## 摘要\n\n## 引言\n\n## 机制与背景\n\n## 临床证据\n\n"
            "## 争议与局限\n\n## 结论与展望\n\n"
        ),
    },
    "systematic": {
        "name": "系统综述（PRISMA 风格）",
        "description": "检索与筛选过程可复现，适合需要交代纳排标准的选题。",
        "markdown": (
            "# 系统综述模板\n\n"
            "## 摘要\n\n## 引言\n\n## 研究问题\n\n## 检索策略\n\n"
            "## 纳入与排除标准\n\n## 结果\n\n## 讨论\n\n## 结论\n\n"
        ),
    },
}


def list_templates(files_dir: Path) -> list[dict[str, Any]]:
    """可选模板：系统默认、内置固定模板、用户上传到项目的模板。"""
    items: list[dict[str, Any]] = [
        {"id": "default", "name": "系统默认（五段式）", "source": "default", "description": "摘要/引言/方法/讨论/结论 + 3 个主题小节。"}
    ]
    for template_id, meta in BUILTIN_TEMPLATES.items():
        items.append(
            {
                "id": template_id,
                "name": meta["name"],
                "source": "builtin",
                "description": meta["description"],
            }
        )
    for path in _uploaded_templates(files_dir):
        items.append(
            {
                "id": f"file:{path.name}",
                "name": path.stem,
                "source": "upload",
                "description": "项目内上传的模板",
            }
        )
    return items


def _uploaded_templates(files_dir: Path) -> list[Path]:
    found: list[Path] = []
    for name in TEMPLATE_NAMES:
        candidate = files_dir / name
        if candidate.is_file():
            found.append(candidate)
    if files_dir.is_dir():
        for candidate in files_dir.iterdir():
            if not candidate.is_file() or candidate.suffix.lower() != ".md":
                continue
            stem = candidate.stem.lower()
            if ("模板" in candidate.stem or "template" in stem) and candidate not in found:
                found.append(candidate)
    return found


def resolve_template(
    choice: str | None,
    files_dir: Path,
    external: "Callable[[str], tuple[list[str], str, str]] | None" = None,
) -> tuple[list[str] | None, str, str]:
    """按“显式选择 → 项目上传 → 系统默认”解析模板，返回 (小节, 名称, 错误)。

    ``external`` 用来解析应用级设置里的 ``upload:`` / ``paper:`` 选择，
    由服务端注入，避免本模块依赖文献库。
    """
    value = (choice or "").strip()
    if value and value not in {"default", "auto"}:
        if value in BUILTIN_TEMPLATES:
            meta = BUILTIN_TEMPLATES[value]
            return sections_from_markdown(meta["markdown"]), meta["name"], ""
        if external is not None and value.split(":", 1)[0] in {"upload", "paper"}:
            return external(value)
        name = value[5:] if value.startswith("file:") else value
        for path in _uploaded_templates(files_dir):
            if path.name == name or path.stem == name:
                return template_sections(path), path.name, ""
        return [], value, f"没有找到模板：{value}"
    uploaded = find_template(files_dir)
    if uploaded is not None:
        return template_sections(uploaded), uploaded.name, ""
    return None, "default-5-section", ""


def _clean_title(value: str) -> str:
    text = _NUMBER_PREFIX.sub("", value.strip())
    return _MARKUP.sub("", text).lower()


def find_template(files_dir: Path) -> Path | None:
    uploaded = _uploaded_templates(files_dir)
    return uploaded[0] if uploaded else None


def template_sections(path: Path) -> list[str]:
    """从模板 Markdown 里取二级标题，作为必需骨架。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return sections_from_markdown(text)


def sections_from_markdown(text: str) -> list[str]:
    titles: list[str] = []
    for line in text.splitlines():
        match = _HEADING.match(line)
        if match and len(match.group(1)) == 2:
            title = match.group(2).strip()
            if title and "参考文献" not in title:
                titles.append(title)
    return titles


def _draft_headings(text: str) -> list[str]:
    titles: list[str] = []
    for line in text.splitlines():
        match = _HEADING.match(line)
        if match:
            titles.append(match.group(2).strip())
    return titles


def _contains(heading: str, keywords: tuple[str, ...]) -> bool:
    cleaned = _clean_title(heading)
    return any(_clean_title(keyword) in cleaned for keyword in keywords)


def evaluate(
    text: str,
    audit: dict[str, Any],
    template: Path | None = None,
    *,
    sections: list[str] | None = None,
    template_label: str = "",
) -> dict[str, Any]:
    """对照基线给草稿打分，返回检查项、差距与下一步动作。"""
    headings = _draft_headings(text)
    metrics = audit.get("metrics") or {}
    section_reports = audit.get("sections") or []

    required: list[tuple[str, str, tuple[str, ...]]] = list(REQUIRED_SECTIONS)
    custom = sections if sections is not None else (template_sections(template) if template else [])
    if custom:
        mapped = []
        for title in custom:
            keywords = (title,)
            for _check_id, _label, known in REQUIRED_SECTIONS:
                if _contains(title, known):
                    keywords = tuple(dict.fromkeys((title, *known)))
                    break
            mapped.append((f"custom:{title}", title, keywords))
        required = mapped

    checks: list[dict[str, Any]] = []
    missing: list[str] = []
    for check_id, label, keywords in required:
        found = any(_contains(heading, keywords) for heading in headings)
        if not found:
            missing.append(label)
        checks.append(
            {
                "id": f"section:{check_id}",
                "label": f"章节「{label}」",
                "current": "有" if found else "缺失",
                "target": "必须存在",
                "passed": found,
                "hint": "" if found else f"补写「{label}」一节，先给结论再给证据。",
            }
        )

    topic_sections = [
        report
        for report in section_reports
        if report.get("title") not in (None, "", "正文")
        and not any(_contains(str(report.get("title")), known) for _, _, known in REQUIRED_SECTIONS)
    ]
    checks.append(
        {
            "id": "topic-sections",
            "label": "主题小节数量",
            "current": len(topic_sections),
            "target": f">= {MIN_TOPIC_SECTIONS}",
            "passed": len(topic_sections) >= MIN_TOPIC_SECTIONS,
            "hint": ""
            if len(topic_sections) >= MIN_TOPIC_SECTIONS
            else "按“机制 / 临床证据 / 争议 / 落地条件”拆出至少 3 个主题小节。",
        }
    )

    weak = [str(r.get("title")) for r in topic_sections if int(r.get("sources") or 0) < MIN_SECTION_SOURCES]
    checks.append(
        {
            "id": "section-sources",
            "label": "每个主题小节来源数",
            "current": f"{len(topic_sections) - len(weak)}/{len(topic_sections)} 达标"
            if topic_sections
            else "无主题小节",
            "target": f">= {MIN_SECTION_SOURCES} 篇/节",
            "passed": bool(topic_sections) and not weak,
            "hint": "为 " + "、".join(weak[:4]) + " 补检索并写证据卡。" if weak else "",
        }
    )

    def metric_check(check_id: str, label: str, current: float | int, target: str, passed: bool, hint: str) -> None:
        checks.append(
            {
                "id": check_id,
                "label": label,
                "current": current,
                "target": target,
                "passed": passed,
                "hint": "" if passed else hint,
            }
        )

    fulltext = int(metrics.get("fulltextSources") or 0)
    metric_check(
        "fulltext",
        "本地全文来源",
        fulltext,
        f">= {MIN_FULLTEXT_SOURCES} 篇",
        fulltext >= MIN_FULLTEXT_SOURCES,
        f"用 academic_search 找 {MIN_FULLTEXT_SOURCES - fulltext} 篇以上，paper_import 导入后 paper_read 精读。",
    )

    citations_total = int(metrics.get("citationsTotal") or 0)
    metric_check(
        "citations",
        "引用总数",
        citations_total,
        f">= {MIN_CITATIONS} 处",
        citations_total >= MIN_CITATIONS,
        "关键论断逐条补引用：本地写（作者 年份, paperId, p页码），摘要来源写 DOI。",
    )

    page_ratio = float(metrics.get("pageRatio") or 0)
    metric_check(
        "page-ratio",
        "页码级引用率",
        f"{round(page_ratio * 100)}%",
        f">= {round(MIN_PAGE_RATIO * 100)}%",
        page_ratio >= MIN_PAGE_RATIO,
        "对关键论断先 paper_read(paperId, page) 再标页码，不要凭摘要猜页码。",
    )

    unresolved = int(metrics.get("unresolved") or 0)
    metric_check(
        "unresolved",
        "未解析引用",
        unresolved,
        "0",
        unresolved == 0,
        "删除或替换本地库里不存在的 paperId。",
    )

    uncited = int(metrics.get("uncitedClaims") or 0)
    metric_check(
        "uncited",
        "无引用论断",
        uncited,
        "0",
        uncited == 0,
        "给这些论断补引用；确实无证据的改写成【待补证据】。",
    )

    passed_checks = sum(1 for check in checks if check["passed"])
    score = round(passed_checks / len(checks) * 100) if checks else 0
    gaps = [f"{check['label']}：{check['current']}（目标 {check['target']}）" for check in checks if not check["passed"]]
    actions = list(dict.fromkeys(check["hint"] for check in checks if check["hint"]))

    return {
        "template": template_label or (template.name if template else "default-5-section"),
        "score": score,
        "passed": not gaps,
        "checks": checks,
        "missingSections": missing,
        "gaps": gaps,
        "actions": actions,
    }
