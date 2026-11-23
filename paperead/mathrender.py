"""LaTeX → 矢量 PDF 片段。

优先使用 MathJax（完整 TeX 引擎，支持 ``aligned`` / ``cases`` / ``bmatrix``
等环境），不可用时回退 matplotlib 的 mathtext（纯 Python、TeX 子集）。
两者都输出单页矢量 PDF，调用方用 ``page.show_pdf_page`` 嵌入。

渲染失败返回 ``None``，调用方回退原公式裁图，不把半截公式或源码写进译文。
"""

from __future__ import annotations

import io
import re
import threading

import pymupdf

from . import mathjax_render

try:  # pragma: no cover - 环境相关
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib.backends.backend_pdf import FigureCanvasPdf
    from matplotlib.figure import Figure

    HAS_MATHTEXT = True
except Exception:  # pragma: no cover - 打包环境缺依赖时走回退
    HAS_MATHTEXT = False

_CACHE: dict[tuple[str, float, str], bytes | None] = {}
_LOCK = threading.Lock()

# Unicode 数学符号 → LaTeX 命令（文本层还原的常见部分）
_UNICODE_MAP = {
    "α": r"\alpha", "β": r"\beta", "γ": r"\gamma", "δ": r"\delta",
    "ε": r"\varepsilon", "ζ": r"\zeta", "η": r"\eta", "θ": r"\theta",
    "ι": r"\iota", "κ": r"\kappa", "λ": r"\lambda", "μ": r"\mu",
    "ν": r"\nu", "ξ": r"\xi", "π": r"\pi", "ρ": r"\rho", "σ": r"\sigma",
    "τ": r"\tau", "υ": r"\upsilon", "φ": r"\phi", "χ": r"\chi",
    "ψ": r"\psi", "ω": r"\omega", "Γ": r"\Gamma", "Δ": r"\Delta",
    "Θ": r"\Theta", "Λ": r"\Lambda", "Ξ": r"\Xi", "Π": r"\Pi",
    "Σ": r"\Sigma", "Φ": r"\Phi", "Ψ": r"\Psi", "Ω": r"\Omega",
    "≤": r"\leq", "≥": r"\geq", "≠": r"\neq", "≈": r"\approx",
    "≡": r"\equiv", "×": r"\times", "÷": r"\div", "±": r"\pm",
    "∓": r"\mp", "∈": r"\in", "∉": r"\notin", "⊂": r"\subset",
    "⊆": r"\subseteq", "∪": r"\cup", "∩": r"\cap", "∑": r"\sum",
    "∏": r"\prod", "∫": r"\int", "∞": r"\infty", "→": r"\to",
    "←": r"\leftarrow", "↔": r"\leftrightarrow", "∂": r"\partial",
    "∇": r"\nabla", "∀": r"\forall", "∃": r"\exists", "⋅": r"\cdot",
    "′": "'", "−": "-", "∼": r"\sim", "∝": r"\propto",
    # 双线体（ℝ/ℕ/ℤ…）：PDF 文本层直接给出 Unicode 字符，需显式映射。
    "ℝ": r"\mathbb{R}", "ℕ": r"\mathbb{N}", "ℤ": r"\mathbb{Z}",
    "ℚ": r"\mathbb{Q}", "ℂ": r"\mathbb{C}", "𝔼": r"\mathbb{E}",
    "⟨": r"\langle", "⟩": r"\rangle", "∥": r"\parallel",
    "⋯": r"\cdots", "…": r"\ldots",
}


def source_to_latex(source: str) -> str:
    """把 PDF 文本层的公式片段尽量还原成 LaTeX（简单公式可用）。"""
    text = (source or "").strip()
    if not text:
        return ""
    for char, command in _UNICODE_MAP.items():
        text = text.replace(char, f" {command} ")
    return _repair_command_glue(" ".join(text.split()))


# 解析层按字符拼 LaTeX 时会把命令与后面的字母粘成一个未知命令：
# ``×`` + ``N`` → ``\timesN``，``\in`` + ``R`` → ``\inR``。MathJax
# 遇到未知命令会渲染成报错文本或整条公式解析失败，最终回退到原文文本。
# 只匹配已知命令名，否则 `\in` 会被回溯切成 `\i`+`n` 误插空格。
_TEX_COMMANDS = sorted(
    {
        value.lstrip("\\")
        for value in _UNICODE_MAP.values()
        if value.startswith("\\")
    }
    | {"text", "mathrm", "mathcal", "mathbb", "left", "right"},
    key=len,
    reverse=True,
)
_CMD_GLUE_RE = re.compile(
    r"\\(" + "|".join(re.escape(name) for name in _TEX_COMMANDS) + r")(?=[A-Za-z])"
)


def _repair_command_glue(latex: str) -> str:
    return _CMD_GLUE_RE.sub(r"\\\1 ", latex or "")


def _normalize(latex: str) -> str:
    value = (latex or "").strip()
    if value.startswith("$$") and value.endswith("$$") and len(value) > 4:
        value = value[2:-2]
    elif value.startswith("$") and value.endswith("$") and len(value) > 2:
        value = value[1:-1]
    return _repair_command_glue(value.strip())


def has_full_engine() -> bool:
    """是否有完整 TeX 引擎（MathJax）。"""
    return mathjax_render.available()


def _mathtext_pdf(body: str, size: float) -> bytes | None:
    if not HAS_MATHTEXT:
        return None
    try:
        figure = Figure(figsize=(0.01, 0.01))
        figure.patch.set_alpha(0)
        figure.text(0, 0, f"${body}$", fontsize=float(size), color="black")
        buffer = io.BytesIO()
        FigureCanvasPdf(figure).print_figure(
            buffer, format="pdf", bbox_inches="tight", pad_inches=0.02
        )
        data = buffer.getvalue()
        probe = pymupdf.open(stream=data, filetype="pdf")
        rect = probe[0].rect
        probe.close()
        if rect.width <= 1 or rect.height <= 1:
            return None
        return data
    except Exception:
        return None


def latex_pdf(latex: str, size: float, *, engine: str = "auto") -> bytes | None:
    """渲染为单页 PDF 字节；失败返回 None（结果按公式+字号+引擎缓存）。

    ``engine``：``auto`` 先 MathJax 再 mathtext；``mathjax`` / ``mathtext``
    指定单一引擎。
    """
    body = _normalize(latex)
    if not body:
        return None

    if engine in ("auto", "mathjax"):
        data = mathjax_render.mathjax_pdf(body, size)
        if data is not None:
            return data
        if engine == "mathjax":
            return None

    if not HAS_MATHTEXT:
        return None
    key = (body, round(float(size), 2), "mathtext")
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]
    data = _mathtext_pdf(body, size)
    with _LOCK:
        _CACHE[key] = data
    return data


def insert_latex(
    page: pymupdf.Page,
    rect: pymupdf.Rect,
    latex: str,
    size: float,
    *,
    center_vertically: bool = True,
) -> bool:
    """把 LaTeX 以矢量形式嵌入 ``rect``；失败返回 False（调用方回退裁图）。"""
    data = latex_pdf(latex, size)
    if not data:
        return False
    doc = None
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
        source = doc[0]
        sw, sh = source.rect.width, source.rect.height
        if sw <= 1 or sh <= 1:
            return False
        scale = min(rect.width / sw, rect.height / sh)
        width, height = sw * scale, sh * scale
        if center_vertically:
            top = rect.y0 + (rect.height - height) / 2
        else:
            top = rect.y1 - height
        target = pymupdf.Rect(rect.x0, top, rect.x0 + width, top + height)
        page.show_pdf_page(target, doc, 0)
        return True
    except Exception:
        return False
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass