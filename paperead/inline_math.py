r"""正文行内公式重建：把 PDF span 的字体/字号/基线还原成 LaTeX。

背景：
    PDF 文本层的段落是拍平的，`\mathcal{P}^{query}_{\mathrm{SPD}(i,j)}` 会变成
    `PquerySPD(i,j)`。仅靠字符串无法还原上下标，但 PyMuPDF 的 span 仍保留
    字体、字号和基线，足以在不调用视觉模型的情况下重建常见行内公式。

约束：
    - 只处理正文里的行内片段，独立公式仍走 equation 块 + 裁图。
    - 规则覆盖不了的结构（分式线、伸缩符号、矩阵等）标记 fallback，
      由解析阶段裁成小图内联显示，保证字母不出错。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 数学字体（含正体 CMR：正文公式里的括号、数字、SPD/log 等）
MATH_FONTS = (
    "CMMI", "CMR", "CMSY", "CMEX", "MSBM", "MTMI", "MTEX", "EUSM", "EUFM", "Symbol",
)
# 这些字体一旦出现，通常意味着分式/大运算符/矩阵等复杂结构，交给裁图兜底
HARD_FONTS = ("CMEX", "MSBM", "MTEX", "EUSM", "EUFM")

GREEK = {
    "α": r"\alpha", "β": r"\beta", "γ": r"\gamma", "δ": r"\delta",
    "ε": r"\epsilon", "ζ": r"\zeta", "η": r"\eta", "θ": r"\theta",
    "ι": r"\iota", "κ": r"\kappa", "λ": r"\lambda", "μ": r"\mu",
    "ν": r"\nu", "ξ": r"\xi", "ρ": r"\rho", "σ": r"\sigma",
    "τ": r"\tau", "υ": r"\upsilon", "φ": r"\phi", "χ": r"\chi",
    "ψ": r"\psi", "ω": r"\omega", "Γ": r"\Gamma", "Δ": r"\Delta",
    "Θ": r"\Theta", "Λ": r"\Lambda", "Σ": r"\Sigma", "Φ": r"\Phi",
    "Ψ": r"\Psi", "Ω": r"\Omega",
}
SYMBOLS = {
    "·": r"\cdot", "×": r"\times", "±": r"\pm", "∓": r"\mp",
    "≤": r"\leq", "≥": r"\geq", "≠": r"\neq", "≈": r"\approx",
    "≡": r"\equiv", "∞": r"\infty", "−": "-", "≪": r"\ll", "≫": r"\gg",
    "∈": r"\in", "∩": r"\cap", "∪": r"\cup", "⊤": r"\top",
    "∂": r"\partial", "∇": r"\nabla", "→": r"\to", "∣": r"\mid",
    "%": r"\%", "∥": r"\parallel", "≃": r"\simeq", "∼": r"\sim",
}
# 明确无法只靠字符还原结构的符号
HARD_CHARS = set("∑∏∫∮√")


@dataclass
class InlineMath:
    start: int
    end: int
    latex: str
    bbox: tuple[float, float, float, float] | None = None
    fallback: bool = False
    source: str = ""

    def to_dict(self) -> dict:
        data = {
            "start": self.start,
            "end": self.end,
            "latex": self.latex,
            "fallback": self.fallback,
            "source": self.source,
        }
        if self.bbox:
            data["bbox"] = [round(v, 1) for v in self.bbox]
        return data


def _is_math(font: str) -> bool:
    return any(font.startswith(prefix) for prefix in MATH_FONTS)


def _is_hard(font: str) -> bool:
    return any(font.startswith(prefix) for prefix in HARD_FONTS)


def _span_base_size(spans: list[dict]) -> float:
    """行内主字号：正文字体优先，否则取出现最多的字号。"""
    prose = [float(s.get("size") or 0) for s in spans if not _is_math(s.get("font") or "")]
    candidates = prose or [float(s.get("size") or 0) for s in spans]
    if not candidates:
        return 10.0
    candidates.sort()
    return candidates[len(candidates) // 2] or 10.0


def _span_baseline(spans: list[dict], base_size: float) -> float:
    """正文基线：取接近主字号、非上下标 span 的 origin y。"""
    ys = [
        float(s["origin"][1])
        for s in spans
        if s.get("origin") and abs(float(s.get("size") or 0) - base_size) < 0.6
    ]
    if not ys:
        ys = [float(s["origin"][1]) for s in spans if s.get("origin")]
    return sum(ys) / len(ys) if ys else 0.0


def _pos(span: dict, base_size: float, baseline: float) -> str:
    size = float(span.get("size") or 0)
    y = float((span.get("origin") or (0, baseline))[1])
    if size < base_size * 0.88:
        return "sup" if y < baseline - 0.6 else "sub"
    return "base"


def _letter(span: dict, text: str) -> str:
    """单段 span 文本 → LaTeX 片段；无法可靠还原时返回空串。"""
    font = span.get("font") or ""
    if not text:
        return ""
    if any(ch in HARD_CHARS for ch in text) or _is_hard(font):
        return ""

    # 省略号在 PDF 里常被拆成多个「·」，合成 \cdots 避免出现 \cdot\cdot\cdot
    stripped = text.strip()
    compact = "".join(ch for ch in stripped if not ch.isspace())
    if len(compact) >= 2 and all(ch == "·" for ch in compact):
        return r"\cdots"

    # 正体：多字母缩写（SPD、Jaccard、log）用 \mathrm；括号/数字保持原样。
    # 必须在字符循环外整体处理，否则每个字符都会重复输出整段缩写。
    if font.startswith("CMR"):
        out: list[str] = []
        for part in re.findall(r"[A-Za-z]{2,}|.", stripped):
            if len(part) > 1 and part.isascii():
                out.append(r"\mathrm{" + part + "}")
            elif part in GREEK:
                out.append(GREEK[part])
            elif part in SYMBOLS:
                out.append(SYMBOLS[part])
            elif not part.isspace():
                out.append(part)
        return "".join(out)

    out = []
    for ch in text:
        if ch.isspace():
            continue
        if ch in GREEK:
            out.append(GREEK[ch])
        elif ch in SYMBOLS:
            out.append(SYMBOLS[ch])
        elif font.startswith("CMSY"):
            # Computer Modern Symbol：大写字母是花体（\mathcal），其余按常见符号
            if ch.isascii() and ch.isalpha() and ch.isupper():
                out.append(r"\mathcal{" + ch + "}")
            else:
                out.append(ch)
        else:
            out.append(ch)
    return "".join(out)


def build_inline_math(spans: list[dict], ranges: list[tuple[int, int, dict]]) -> list[InlineMath]:
    """从一行 span 里找出行内公式并重建 LaTeX。

    Args:
        spans: PyMuPDF 的 span 列表（按阅读顺序）。
        ranges: `(start, end, span)`，start/end 是该 span 在行文本中的字符偏移。
    """
    if not ranges:
        return []
    base_size = _span_base_size(spans)
    baseline = _span_baseline(spans, base_size)

    out: list[InlineMath] = []
    group: list[tuple[int, int, dict]] = []

    def flush() -> None:
        nonlocal group
        if not group:
            return
        # 只有数学字体才构成行内公式；纯正体正文不处理
        if not any(_is_math(s.get("font") or "") for _, _, s in group):
            group = []
            return
        first = next(i for i, (_, _, s) in enumerate(group) if _is_math(s.get("font") or ""))
        last = max(i for i, (_, _, s) in enumerate(group) if _is_math(s.get("font") or ""))
        group = group[first:last + 1]
        start = group[0][0]
        end = group[-1][1]
        raw = "".join((s.get("text") or "") for _, _, s in group)
        lead = len(raw) - len(raw.lstrip())
        trail = len(raw) - len(raw.rstrip())
        start += lead
        end -= trail
        raw = raw.strip()

        atoms: list[dict] = []
        hard = False
        for _, _, span in group:
            text = (span.get("text") or "").strip()
            if not text:
                continue
            pos = _pos(span, base_size, baseline)
            tex = _letter(span, text)
            if not tex:
                hard = True
                break
            if pos == "base":
                atoms.append({"tex": tex, "sup": "", "sub": ""})
            else:
                if not atoms:
                    hard = True
                    break
                atoms[-1][pos] += tex
        if hard or not atoms:
            bbox = None
            xs = [s["bbox"] for _, _, s in group]
            if xs:
                bbox = (
                    min(b[0] for b in xs), min(b[1] for b in xs),
                    max(b[2] for b in xs), max(b[3] for b in xs),
                )
            out.append(InlineMath(start, end, "", bbox, True, raw))
        else:
            pieces = []
            for atom in atoms:
                piece = atom["tex"]
                if atom["sup"]:
                    piece += "^{" + atom["sup"] + "}"
                if atom["sub"]:
                    piece += "_{" + atom["sub"] + "}"
                pieces.append(piece)
            bbox = (
                min(s["bbox"][0] for _, _, s in group),
                min(s["bbox"][1] for _, _, s in group),
                max(s["bbox"][2] for _, _, s in group),
                max(s["bbox"][3] for _, _, s in group),
            )
            out.append(InlineMath(start, end, " ".join(pieces), bbox, False, raw))
        group = []

    for index, item in enumerate(ranges):
        _, _, span = item
        if _is_math(span.get("font") or ""):
            group.append(item)
            continue
        # 正文 span：数学片段结束（公式紧跟的逗号/句号留在正文里）
        flush()

    flush()
    return out


def spans_with_offsets(spans: list[dict]) -> tuple[str, list[tuple[int, int, dict]]]:
    """按 `_join_spans` 的规则拼文本，同时返回每个 span 的字符区间。"""
    parts: list[str] = []
    ranges: list[tuple[int, int, dict]] = []
    cursor = 0
    prev_x1: float | None = None
    for span in spans:
        text = span.get("text") or ""
        if not text:
            continue
        x0, x1 = span["bbox"][0], span["bbox"][2]
        size = float(span.get("size") or 10.0)
        if parts and prev_x1 is not None:
            gap = x0 - prev_x1
            if (
                gap > max(0.18 * size, 1.0)
                and not parts[-1].endswith((" ", "-", "/", "\u00ad"))
                and not text.startswith(" ")
            ):
                parts.append(" ")
                cursor += 1
        start = cursor
        parts.append(text)
        cursor += len(text)
        ranges.append((start, cursor, span))
        prev_x1 = x1
    return "".join(parts), ranges