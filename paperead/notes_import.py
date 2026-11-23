"""外部论文笔记（mkdocs markdown）导入。

只做解析：把一篇笔记 md 转成与 PDF 解析产物同构的 ``Block`` 列表，
后续可直接复用检索、问答材料与前端 Markdown 渲染。

笔记来源：https://github.com/M0rtzz/paper-notes（上游 zhaoyang97/Paper-Notes），
内容许可 CC BY-NC-SA 4.0，因此这里不内置任何笔记正文，只提供本地导入能力。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path


NOTE_SOURCE = "paper-notes"
NOTE_LICENSE = "CC BY-NC-SA 4.0"
NOTE_ORIGIN = "https://github.com/M0rtzz/paper-notes"

# 笔记固定小节 -> 语义。前端可据此把六段式映射到导读面板。
SECTION_ROLES = {
    "一句话总结": "oneliner",
    "研究背景与动机": "motivation",
    "方法详解": "method",
    "实验关键数据": "experiments",
    "亮点与洞察": "insights",
    "局限与展望": "limitations",
    "相关工作与启发": "related",
}

_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_HTML_BLOCK_RE = re.compile(r"<div\b[^>]*>.*?</div>", re.S | re.I)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_LEADING_TAG_RE = re.compile(r"^\[[^\]]{1,24}\]\s*")


@dataclass
class NoteBlock:
    """与 ``paperead.layout.Block`` 同构的轻量块；笔记没有 PDF 坐标，不走 pymupdf。"""

    id: str
    order: int
    page: int
    kind: str
    text: str
    bbox: tuple[float, float, float, float]
    level: int = 0
    section: str = ""
    translatable: bool = True
    translated: str = ""
    caption: str = ""
    image_path: str | None = None
    table_html: str = ""
    latex: str = ""
    eq_number: str = ""
    column_role: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "order": self.order,
            "page": self.page,
            "kind": self.kind,
            "level": self.level,
            "section": self.section,
            "text": self.text,
            "caption": self.caption,
            "image_path": self.image_path,
            "table_html": self.table_html,
            "latex": self.latex,
            "eq_number": self.eq_number,
            "translatable": self.translatable,
            "translated": self.translated,
            "column_role": self.column_role,
            "bbox": [round(v, 1) for v in self.bbox],
        }


@dataclass
class Note:
    """一篇已解析的外部笔记。"""

    slug: str
    title: str
    conference: str = ""
    topic: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    source_path: str = ""
    source_url: str = ""
    blocks: list[NoteBlock] = field(default_factory=list)

    @property
    def sections(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for block in self.blocks:
            role = SECTION_ROLES.get(block.section)
            if not role:
                continue
            out.setdefault(role, "")
            if block.kind in ("para", "table", "equation") and block.text:
                out[role] = (out[role] + "\n\n" + block.text).strip()
        return out

    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "title": self.title,
            "conference": self.conference,
            "field": self.topic,
            "description": self.description,
            "tags": self.tags,
            "source_path": self.source_path,
            "source_url": self.source_url,
            "source": NOTE_SOURCE,
            "license": NOTE_LICENSE,
            "origin": NOTE_ORIGIN,
            "sections": self.sections,
            "blocks": [b.to_dict() for b in self.blocks],
        }


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _parse_front_matter(text: str) -> tuple[dict, str]:
    """解析极简 YAML front-matter；只支持笔记仓库实际用到的几种写法。"""
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n")
    end = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end < 0:
        return {}, text

    meta: dict = {}
    key = ""
    buffered: list[str] = []
    is_list = False

    def flush() -> None:
        nonlocal buffered
        if not key:
            buffered = []
            return
        if is_list:
            meta[key] = list(buffered)
        elif buffered:
            meta[key] = " ".join(part.strip() for part in buffered if part.strip())
        buffered = []

    for raw in lines[1:end]:
        if not raw.strip():
            continue
        stripped = raw.strip()
        if stripped.startswith("- ") and key:
            if not is_list:
                is_list = True
                buffered = []
            buffered.append(_unquote(stripped[2:]))
            continue
        match = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", stripped)
        if match and not raw.startswith((" ", "\t")):
            flush()
            key, rest = match.group(1), match.group(2).strip()
            is_list = False
            buffered = []
            if rest in (">-", ">", "|", "|-", ">+"):
                continue
            if rest:
                meta[key] = _unquote(rest)
            continue
        if key and buffered is not None:
            buffered.append(_unquote(stripped))

    flush()
    return meta, "\n".join(lines[end + 1 :])


def _strip_noise(text: str) -> str:
    text = _COMMENT_RE.sub("", text)
    text = _HTML_BLOCK_RE.sub("", text)
    return text


def _conference_of(parts: list[str]) -> str:
    for part in parts:
        if re.fullmatch(r"[A-Z]{2,8}\d{4}", part):
            return part
    return ""


def _topic_of(parts: list[str]) -> str:
    conference = _conference_of(parts)
    if not conference:
        return ""
    index = parts.index(conference)
    return parts[index + 1] if index + 1 < len(parts) else ""


def _clean_title(raw: str) -> str:
    title = raw.strip()
    while True:
        stripped = _LEADING_TAG_RE.sub("", title)
        if stripped == title:
            return title
        title = stripped


def _make_block(index: int, kind: str, text: str, section: str, level: int = 0) -> NoteBlock:
    return NoteBlock(
        id=f"n{index:04d}",
        order=index,
        page=1,
        kind=kind,
        text=text.strip(),
        bbox=(0.0, 0.0, 0.0, 0.0),
        level=level,
        section=section,
        translatable=kind not in ("figure", "table"),
    )


def _blocks_from_body(body: str) -> list[NoteBlock]:
    blocks: list[Block] = []
    section = ""
    buffer: list[str] = []
    index = 0

    def push(kind: str, text: str, level: int = 0) -> None:
        nonlocal index
        if not text.strip():
            return
        blocks.append(_make_block(index, kind, text, section, level))
        index += 1

    def flush_para() -> None:
        nonlocal buffer
        text = "\n".join(buffer).strip()
        buffer = []
        if not text:
            return
        if text.startswith("$$") and text.endswith("$$") and len(text) > 4:
            push("equation", text)
        elif all(line.lstrip().startswith("|") for line in text.split("\n") if line.strip()):
            push("table", text)
        else:
            push("para", text)

    lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    fence = ""
    in_fence = False

    for raw in lines:
        line = raw.rstrip()
        fence_match = _FENCE_RE.match(line)
        if fence_match:
            if not in_fence:
                flush_para()
                in_fence = True
                fence = fence_match.group(1)
            elif line.strip().startswith(fence):
                # 闭合行必须留在块里：拼回 markdown 时围栏未闭合，
                # 后续整篇内容都会被当成代码块。
                buffer.append(line)
                in_fence = False
                push("para", "\n".join(buffer))
                buffer = []
            if in_fence:
                buffer.append(line)
            continue

        if in_fence:
            buffer.append(line)
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            flush_para()
            level = len(heading.group(1))
            title = heading.group(2).strip()
            if level == 1:
                push("title", title, 0)
            else:
                # 小节归属只认 H2：H3 是「方法详解」等小节内部的子标题，
                # 若让它覆盖 section，六段式小节就会取不到内容。
                if level == 2:
                    section = title
                push("heading", title, level)
            continue

        if not line.strip():
            flush_para()
            continue

        buffer.append(line)

    flush_para()
    return blocks


def parse_note(path: str | Path, root: str | Path | None = None) -> Note:
    """解析单篇笔记 md。``root`` 用于从相对路径推断会议与领域。"""
    file = Path(path)
    raw = file.read_text(encoding="utf-8")
    meta, body = _parse_front_matter(raw)

    rel = file
    if root is not None:
        try:
            rel = file.resolve().relative_to(Path(root).resolve())
        except (OSError, ValueError):
            rel = file
    parts = list(rel.parts[:-1]) if rel.parts else []

    tags = [str(tag) for tag in (meta.get("tags") or []) if str(tag).strip()]
    title = _clean_title(str(meta.get("title") or file.stem))
    conference = _conference_of(parts) or _conference_of(tags)
    topic = _topic_of(parts)

    return Note(
        slug=file.stem,
        title=title,
        conference=conference,
        topic=topic,
        description=str(meta.get("description") or "").strip(),
        tags=tags,
        source_path=str(rel).replace("\\", "/"),
        blocks=_blocks_from_body(_strip_noise(body)),
    )


def collect_notes(root: str | Path, limit: int = 0) -> list[Note]:
    """扫描 docs 目录下的全部笔记；``limit`` 大于 0 时只取前若干篇。"""
    base = Path(root)
    files = sorted(
        p for p in base.rglob("*.md") if p.name.lower() not in ("index.md", "readme.md")
    )
    if limit > 0:
        files = files[:limit]
    return [parse_note(p, root=base) for p in files]


def main(argv: list[str] | None = None) -> int:
    import sys

    args = list(argv if argv is not None else sys.argv[1:])
    if not args:
        print("用法: python -m paperead.notes_import <笔记md或docs目录> [--json 输出路径] [--limit N]")
        return 2

    target = Path(args[0])
    out = ""
    limit = 0
    if "--json" in args:
        out = args[args.index("--json") + 1]
    if "--limit" in args:
        limit = int(args[args.index("--limit") + 1])

    notes = collect_notes(target, limit=limit) if target.is_dir() else [parse_note(target)]
    result = [note.to_dict() for note in notes]

    print(f"解析笔记 {len(result)} 篇")
    if result:
        first = result[0]
        kinds: dict[str, int] = {}
        for block in first["blocks"]:
            kinds[block["kind"]] = kinds.get(block["kind"], 0) + 1
        print(f"样例: {first['conference']}/{first['field']} · {first['title']}")
        print(f"小节: {', '.join(first['sections'].keys())}")
        print(f"块数: {len(first['blocks'])} · 分布 {json.dumps(kinds, ensure_ascii=False)}")
        print(f"许可: {first['license']} · 来源 {first['origin']}")
    if out:
        Path(out).write_text(
            json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())