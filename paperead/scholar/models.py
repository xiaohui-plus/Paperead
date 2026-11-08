"""检索层的通用模型与文本清洗。

题录来自不同数据源，形态差异很大：
- arXiv 的标题/摘要是 LaTeX 源码片段（`\\cite{}`、`$x$`），
- Europe PMC / PubMed 的摘要带 HTML 小标题（`<h4>Background</h4>`），
- OpenAlex 的摘要是「词 → 位置」的倒排索引。
这里把清洗函数集中起来，让上层只面对纯文本。
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field, fields
from typing import Any, Literal

Source = Literal["arxiv", "pubmed", "openalex", "europepmc"]

SOURCE_LABELS: dict[str, str] = {
    "arxiv": "arXiv",
    "pubmed": "PubMed",
    "openalex": "OpenAlex",
    "europepmc": "Europe PMC",
}

# 界面上的数据源顺序与说明（详情见前端 SOURCE_META）
SOURCES: list[tuple[str, str]] = [
    ("arxiv", "物理、数学、计算机等预印本"),
    ("pubmed", "生物医学与生命科学文献"),
    ("openalex", "全学科开放学术图谱，附带被引量"),
    ("europepmc", "生物医学全文、预印本与被引量"),
]

ARXIV_FIELDS: list[tuple[str, str]] = [
    ("all", "全部字段"),
    ("title", "标题"),
    ("abstract", "摘要"),
    ("author", "作者"),
]

# arXiv 最早收录年份，作为年份区间的默认起点。
ARXIV_EPOCH_YEAR = 1991


@dataclass
class Paper:
    """一条题录。字段与前端一致，`to_dict` 直接作为 API 响应。"""

    id: str  # "arxiv:2401.00001" / "openalex:W123" / "europepmc:MED/123" / "pubmed:12345"
    source: str
    title: str
    authors: list[str] = field(default_factory=list)
    venue: str = ""
    year: int | None = None
    category: str = ""
    keywords: list[str] = field(default_factory=list)
    abstract: str = ""
    url: str = ""
    pdf_url: str | None = None
    html_url: str | None = None
    published_at: str | None = None
    doi: str | None = None
    citations: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "title": self.title,
            "authors": list(self.authors),
            "venue": self.venue,
            "year": self.year,
            "category": self.category,
            "keywords": list(self.keywords),
            "abstract": self.abstract,
            "url": self.url,
            "pdf_url": self.pdf_url,
            "html_url": self.html_url,
            "published_at": self.published_at,
            "doi": self.doi,
            "citations": self.citations,
        }


def to_paper_dict(item: Paper | dict[str, Any]) -> dict[str, Any]:
    return item.to_dict() if isinstance(item, Paper) else dict(item)


def paper_from_dict(data: dict[str, Any]) -> Paper:
    """从前端回传的题录重建 Paper（只认已知字段，缺标题/来源时给安全默认值）。"""
    allowed = {f.name for f in fields(Paper)}
    payload = {k: v for k, v in (data or {}).items() if k in allowed}
    payload.setdefault("id", "")
    payload.setdefault("source", "arxiv")
    payload.setdefault("title", "")
    if not isinstance(payload.get("authors"), list):
        payload["authors"] = []
    if not isinstance(payload.get("keywords"), list):
        payload["keywords"] = []
    return Paper(**payload)


# ── 文本清洗 ────────────────────────────────────────────────


def decode_xml(text: str) -> str:
    """去掉标签与 CDATA，还原实体，压平空白。"""
    if not text:
        return ""
    s = re.sub(r"<!\[CDATA\[([\s\S]*?)\]\]>", r"\1", text)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


# \cite{KL}、\ref{eq:1} 这类宏离开参考文献表/编号体系就只是乱码，整条剔除。
# 只认这些「指针」宏：\sigma、\mathbb{R} 等数学宏与 $...$ 公式一律不碰。
_LATEX_POINTER_RE = re.compile(
    r"(?:~\s*)?\\(?:nocite|cite[a-zA-Z]*|ref|eqref|autoref|Cref|cref|label)"
    r"\*?(?:\[[^\]]*\])*(?:\{[^{}]*\}|\{[^{}\s]*)"
)


def strip_latex_pointers(text: str) -> str:
    if not text or "\\" not in text:
        return text
    s = _LATEX_POINTER_RE.sub("", text)
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r"[ \t]+([,.;:!?)])", r"\1", s)
    return s.strip()


def clean_html_text(text: str) -> str:
    """Europe PMC / PubMed 摘要里的 HTML 小标题还原成换行分节。"""
    if not text:
        return ""
    s = re.sub(r"<\s*br\s*/?\s*>", "\n", text, flags=re.I)
    s = re.sub(r"<\s*/\s*(h[1-6]|p|div|section)\s*>", "\n", s, flags=re.I)
    s = re.sub(r"<\s*li[^>]*>", "· ", s, flags=re.I)
    s = re.sub(r"<[^>]*>", "", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return strip_latex_pointers(s.strip())


def reconstruct_abstract(inverted: dict[str, list[int]] | None) -> str:
    """OpenAlex 的摘要倒排索引还原成句子。"""
    if not inverted:
        return ""
    slots: dict[int, str] = {}
    for word, positions in inverted.items():
        for pos in positions or []:
            try:
                slots[int(pos)] = word
            except (TypeError, ValueError):
                continue
    return " ".join(slots[i] for i in sorted(slots))


def strip_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    bare = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi.strip(), flags=re.I)
    return bare or None


def epmc_key(src: str, record_id: str) -> str:
    """Europe PMC 的唯一性取决于 (source, id)，如 MED/26780180、PMC/PMC4744125。"""
    return f"europepmc:{src}/{record_id}"


def split_epmc_key(raw: str) -> tuple[str, str] | None:
    i = raw.find("/")
    if i <= 0 or i >= len(raw) - 1:
        return None
    return raw[:i], raw[i + 1 :]


def epmc_pdf_url(record: dict[str, Any]) -> str | None:
    """从 fullTextUrlList 挑出「开放获取 + PDF」的直链。"""
    for item in ((record.get("fullTextUrlList") or {}).get("fullTextUrl") or []):
        if item.get("availabilityCode") == "OA" and item.get("documentStyle") == "pdf":
            return item.get("url") or None
    return None


def normalize_year(value: Any) -> int | None:
    try:
        year = int(str(value)[:4])
    except (TypeError, ValueError):
        return None
    return year if 1000 <= year <= 2200 else None


def truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
