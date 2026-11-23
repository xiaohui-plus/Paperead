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
import unicodedata
from dataclasses import dataclass, field

from .fonts.registry import is_anchor_font, is_aux_font, is_math_font
from .textnorm import keep_space_between

# 强数学字体：只有这些字体能作为「这一行是公式」的锚点。
# CMR 是 Computer Modern 的**正文字体**（AASTeX / 部分 arXiv 论文整篇正文都用
# CMR10），绝不能单独作为公式证据——否则整篇正文都会被重建为行内公式。
STRONG_MATH_FONTS = (
    "CMMI", "CMSY", "CMEX", "MSBM", "MTMI", "MTEX", "EUSM", "EUFM", "Symbol",
    # STIX 数学字体家族（MDPI/Elsevier 常见）：数学斜体、花体、粗体都在
    # STIXMath* 下，只认 Computer Modern 会把 𝑖/𝑁 与 PUA 花体漏成正文方块。
    "STIXMath",
)
# 辅助字体：只能并入已经由强数学字体开启的公式组（括号、数字、Mpc/SPD 等正体词）。
AUX_MATH_FONTS = ("CMR",)
MATH_FONTS = STRONG_MATH_FONTS + AUX_MATH_FONTS
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
    # 常见关系/算子：KaTeX 未必认识原 Unicode，必须显式映射为命令。
    "≜": r"\triangleq", "⊆": r"\subseteq", "⊂": r"\subset",
    "⊇": r"\supseteq", "⊃": r"\supset", "⊕": r"\oplus",
    "⊗": r"\otimes", "⟂": r"\perp", "⋯": r"\cdots", "⋅": r"\cdot",
    "≅": r"\cong", "∘": r"\circ", "∅": r"\varnothing",
    "∀": r"\forall", "∃": r"\exists", "¬": r"\neg",
    "∧": r"\wedge", "∨": r"\vee", "∝": r"\propto",
    "⇒": r"\Rightarrow", "⇔": r"\Leftrightarrow", "↦": r"\mapsto",
    "∖": r"\setminus", "≺": r"\prec", "≻": r"\succ",
    "⟨": r"\langle", "⟩": r"\rangle", "〈": r"\langle", "〉": r"\rangle",
    "‖": r"\Vert", "⌈": r"\lceil", "⌉": r"\rceil",
    "⌊": r"\lfloor", "⌋": r"\rfloor", "…": r"\ldots",
    "′": "'", "″": "''",
    # 常见双线体字母：NFKC 会把 𝔼 归一成 E，先按原字符映射成 \mathbb。
    "ℝ": r"\mathbb{R}", "ℕ": r"\mathbb{N}", "ℤ": r"\mathbb{Z}",
    "ℚ": r"\mathbb{Q}", "ℂ": r"\mathbb{C}", "𝔼": r"\mathbb{E}",
}
# 明确无法只靠字符还原结构的符号
HARD_CHARS = set("∑∏∫∮√")
# 列表项目符号会以 Symbol 字体出现，被误判成行内公式后只能裁图；它们没有
# 数学语义，应留在正文里当普通字符（渲染层再替换成有字形的等价符号）。
DECORATIVE_CHARS = set("◦•‣▪▫○●◆◇")
# PDF 字体把数学字形塞进私用区（STIX PUA）或 ToUnicode 失败时留下替换符；
# 前端没有对应字形，会渲染成「□」。这类字符不能猜，只能交给识图或裁图垫后。
ABNORMAL_RE = re.compile(r"[\ue000-\uf8ff\ufffd]")


def _norm_math_char(ch: str) -> str:
    """Unicode 数学字母区（𝑈/𝑖/𝑁）归一化成可渲染的 ASCII。

    `unicodedata.normalize('NFKC', '𝑖')` → `i`，避免把 U+1D456 原样塞进
    LaTeX（KaTeX 未必认识）或留下缺字形方块。
    """
    if 0x1D400 <= ord(ch) <= 0x1D7FF or 0x2100 <= ord(ch) <= 0x214F:
        return unicodedata.normalize("NFKC", ch)
    return ch


@dataclass
class InlineMath:
    start: int
    end: int
    latex: str
    bbox: tuple[float, float, float, float] | None = None
    fallback: bool = False
    source: str = ""
    # 异常符号兜底（私用区/替换符）不能参与公式碎片合并，否则会把相邻
    # 正文词一起裁进小图，出现「and the rest of」这种正文被截图覆盖。
    symbol: bool = False

    def to_dict(self) -> dict:
        data = {
            "start": self.start,
            "end": self.end,
            "latex": self.latex,
            "fallback": self.fallback,
            "source": self.source,
        }
        if self.symbol:
            data["symbol"] = True
        if self.bbox:
            data["bbox"] = [round(v, 1) for v in self.bbox]
        return data


def _is_math(font: str) -> bool:
    # 角色由 fonts.registry 统一解析；历史前缀表仍保留给外部导入兼容。
    return is_math_font(font)


def _is_strong_math(font: str) -> bool:
    return is_anchor_font(font)


def _is_aux_math(font: str) -> bool:
    return is_aux_font(font)


def _is_hard(font: str) -> bool:
    return any(font.startswith(prefix) for prefix in HARD_FONTS)


def _span_base_size(spans: list[dict]) -> float:
    """行内主字号：正文字体优先，否则取出现最多的字号。"""
    prose = [
        float(s.get("size") or 0)
        for s in spans
        if not _is_strong_math(s.get("font") or "")
    ]
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


_TEX_COMMANDS = sorted(
    {
        value.lstrip("\\")
        for value in list(GREEK.values()) + list(SYMBOLS.values())
        if value.startswith("\\")
    }
    | {"text", "mathrm", "mathcal", "mathbb"},
    key=len,
    reverse=True,
)
# 只认已知命令名，避免把 `\in` 回溯切成 `\i`+`n` 这种误伤。
_TEX_GLUE_RE = re.compile(
    r"\\(" + "|".join(re.escape(name) for name in _TEX_COMMANDS) + r")(?=[A-Za-z])"
)


def _join_tex(parts: list[str]) -> str:
    r"""拼 LaTeX 片段：命令与后续字母之间补空格。

    ``×``→``\times`` 后紧跟 ``N`` 会粘成 ``\timesN``，``\in`` 后紧跟 ``R``
    粘成 ``\inR``。MathJax 把它们当未知命令，整条公式渲染失败后退回原文
    文本，白白丢掉矢量化机会。这里只对已知命令名匹配，不会破坏 ``\in``。
    """
    return _TEX_GLUE_RE.sub(r"\\\1 ", "".join(parts))


def _letter(span: dict, text: str) -> str:
    """单段 span 文本 → LaTeX 片段；无法可靠还原时返回空串。"""
    font = span.get("font") or ""
    if not text:
        return ""
    # 私用区字形没有可靠的 Unicode 语义：硬猜会把「𝒰」猜成错误字母。
    # 返回空串让调用方把整段标成 fallback，由裁图/识图垫后。
    if ABNORMAL_RE.search(text):
        return ""
    text = "".join(_norm_math_char(ch) for ch in text)
    if any(ch in HARD_CHARS for ch in text):
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
        return _join_tex(out)

    out = []
    for ch in text:
        if ch.isspace():
            continue
        # 双线体/特殊符号先按原字符映射，再做 Unicode 数学字母归一化。
        if ch in SYMBOLS:
            out.append(SYMBOLS[ch])
            continue
        ch = _norm_math_char(ch)
        if ch in GREEK:
            out.append(GREEK[ch])
        elif font.startswith("CMSY"):
            # Computer Modern Symbol：大写字母是花体（\mathcal），其余按常见符号
            if ch.isascii() and ch.isalpha() and ch.isupper():
                out.append(r"\mathcal{" + ch + "}")
            else:
                out.append(ch)
        elif not ch.isascii():
            # 数学字体里出现韩文/私用区/未分配码点，说明 PDF 的 ToUnicode
            # 映射错乱（实测数学斜体 i 被映射成韩文 U+D456），前端没有
            # 对应字形。把乱码塞进 LaTeX 只会渲染成方块，交给裁图兜底。
            if unicodedata.category(ch) in ("Co", "Cn", "Lo", "Cs"):
                return ""
            # 未收录的非 ASCII 运算符（KaTeX 不保证支持原字符）同样不猜。
            return ""
        else:
            out.append(ch)
    return _join_tex(out)


def build_symbol_fallbacks(
    text: str,
    ranges: list[tuple[int, int, dict]],
    existing: list[dict] | None = None,
) -> list[InlineMath]:
    """把正文里无法渲染的私用区/替换符片段标成 fallback。

    这些字符在前端没有字形，会显示成「□」。`build_inline_math` 只处理
    「强数学字体组」；正文里孤立出现的异常符号（例如 STIX 花体 U）不在
    公式组内，必须单独补一个裁图区间，否则整段正文永久带着方块。
    """
    covered = [
        (int(m.get("start", 0)), int(m.get("end", 0)))
        for m in (existing or [])
    ]
    out: list[InlineMath] = []
    for start, end, span in ranges:
        if end <= start:
            continue
        seg = text[start:end] or (span.get("text") or "")
        if not ABNORMAL_RE.search(seg):
            continue
        if any(a < end and b > start for a, b in covered):
            continue
        bbox = span.get("bbox")
        out.append(
            InlineMath(
                start, end, "", tuple(bbox) if bbox else None, True, seg, symbol=True,
            )
        )
    return out


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
        # 只有强数学字体才能构成行内公式。正文字体（CMR）即使整行都是，
        # 也不能单独成组——AASTeX 等 LaTeX 论文的正文就是 CMR10。
        if not any(_is_strong_math(s.get("font") or "") for _, _, s in group):
            group = []
            return
        start = group[0][0]
        end = group[-1][1]
        raw = "".join((s.get("text") or "") for _, _, s in group)
        lead = len(raw) - len(raw.lstrip())
        trail = len(raw) - len(raw.rstrip())
        start += lead
        end -= trail
        raw = raw.strip()

        # 整组只是列表项目符号：不是公式，留在正文当普通字符。
        if raw and all(ch in DECORATIVE_CHARS for ch in raw):
            group = []
            return

        atoms: list[dict] = []
        hard = False
        for _, _, span in group:
            text = (span.get("text") or "").strip()
            if not text:
                continue
            pos = _pos(span, base_size, baseline)
            if _is_math(span.get("font") or ""):
                tex = _letter(span, text)
            else:
                # 并入公式的正体词（front/down）按 \text 保留正体
                tex = r"\text{" + text + "}"
            if not tex:
                hard = True
                break
            if pos == "base":
                atoms.append({"tex": tex, "sup": "", "sub": ""})
            else:
                if not atoms:
                    # 组里没有基字符（孤立的上/下标，常见于公式被正文
                    # span 切断）：按普通字符保留即可。为丢失上标位置
                    # 而整组裁图，会让正文出现大量小图，代价更大。
                    atoms.append({"tex": tex, "sup": "", "sub": ""})
                    continue
                atoms[-1][pos] += tex
        if hard or not atoms:
            bbox = None
            xs = [s["bbox"] for _, _, s in group]
            if xs:
                bbox = (
                    min(b[0] for b in xs), min(b[1] for b in xs),
                    max(b[2] for b in xs), max(b[3] for b in xs),
                )
            out.append(InlineMath(start, end, "", bbox, True, raw, symbol=bool(ABNORMAL_RE.search(raw))))
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
        font = span.get("font") or ""
        text = (span.get("text") or "").strip()
        if _is_strong_math(font):
            group.append(item)
            continue
        # 组已经由强数学字体开启时，才允许并入**紧邻的短辅助片段**：
        # 公式里的括号/数字（CMR）、Mpc/kpc/K 这类单位正体词。
        # 不能按「CMR 就是公式」处理，否则一行正文会被整行吞掉。
        aux_ok = (
            bool(group)
            and _is_aux_math(font)
            and text
            and len(text) <= 12
            and " " not in text
            and (
                any(ch.isdigit() for ch in text)
                or text.isupper()
                or not re.search(r"[a-z]{4,}", text)
            )
        )
        if aux_ok:
            group.append(item)
            continue
        # 公式里的正体短词（x^front / x^down 的 front、down）夹在两个
        # 强数学 span 之间；不并入会把一条公式切成三段。
        prev_strong = bool(group) and _is_strong_math(group[-1][2].get("font") or "")
        next_strong = (
            index + 1 < len(ranges)
            and _is_strong_math(ranges[index + 1][2].get("font") or "")
        )
        if (
            group
            and prev_strong
            and next_strong
            and text
            and len(text) <= 12
            and text.isascii()
            and text.isalpha()
        ):
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
                and keep_space_between(parts[-1], text)
            ):
                parts.append(" ")
                cursor += 1
        start = cursor
        parts.append(text)
        cursor += len(text)
        ranges.append((start, cursor, span))
        prev_x1 = x1
    return "".join(parts), ranges