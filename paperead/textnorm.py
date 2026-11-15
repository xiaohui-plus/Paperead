"""CJK 文本的空格规则。

PDF 的中文正文常常一行就是一个 Line，而版面重排里的跨行合并、span 间距补白
都沿用西文「词间留空格」的规则，结果在汉字之间插进多余空格。这里集中定义
判定，避免各处实现漂移。
"""

from __future__ import annotations

import re

# 汉字、假名、谚文、中文标点与全角字符。中文标点也按 CJK 处理：
# 「，」「。」两侧不应因为换行或 span 间距被拆出一个空格。
_CJK_CLASS = (
    "\u2e80-\u2eff"    # CJK 部首补充
    "\u3000-\u303f"    # 中文标点（、。《》「」等）与全角空格
    "\u3040-\u30ff"    # 日文假名
    "\u3400-\u4dbf"    # 扩展 A
    "\u4e00-\u9fff"    # 基本汉字
    "\uac00-\ud7af"    # 韩文
    "\uf900-\ufaff"    # 兼容汉字
    "\ufe30-\ufe4f"    # 兼容标点
    "\uff00-\uffef"    # 全角字符（，！？：；（）ＡＢＣ）
)
_CJK_RE = re.compile(f"[{_CJK_CLASS}]")
# CJK 之间由 PDF 文本层或 OCR 带入的空白（半角空格、制表、全角空格）
_CJK_GAP_RE = re.compile(f"(?<=[{_CJK_CLASS}])[ \t\u3000]+(?=[{_CJK_CLASS}])")


def is_cjk(ch: str) -> bool:
    """单个字符是否属于 CJK 语境。"""
    return bool(ch) and _CJK_RE.match(ch) is not None


def keep_space_between(left: str, right: str) -> bool:
    """相邻两段文本之间是否要保留一个空格。

    CJK 与 CJK 直接相接；中英混排、公式与正文之间仍按西文规则留空格。
    """
    if not left or not right:
        return False
    return not (is_cjk(left[-1]) and is_cjk(right[0]))


# ── 目录引导符（dot leader）────────────────────────────────────────────
# 目录页用一串点把标题和页码连起来。PyMuPDF 常把每个点拆成独立 span，
# 版面重排后按字距补空格，就成了 ". . . . . . . ."；这段文本再被送去
# 翻译时，模型会原样复制点线，阅读页就出现大片「原点」。
# 只认 4 个以上（允许点之间夹空格）的连续点，正文里的省略号 "..." 与 "…"
# 不受影响。
_DOT_CHARS = ".．﹒·⋅•"
_DOT_LEADER_RE = re.compile(r"(?:[ \t]*[.．﹒·⋅•]){4,}|…{2,}")
_SPACE_RUN_RE = re.compile(r"[ \t]{2,}")
_DOT_ONLY_CORE_RE = re.compile(r"[ \t\d.．﹒·⋅•\-–—_()[\]{}]")
_DOT_CHAR_RE = re.compile(f"[{re.escape(_DOT_CHARS)}]")
# 引导符之后若只剩页码（可带括号/连字符），说明它确实是目录条目
_DOT_TAIL_RE = re.compile(r"[\s\d\-–—()\[\]{}]{0,8}")
# 引导符之后紧跟的页码：目录条目最常见的形态「标题 . . . . 17」
_DOT_PAGE_RE = re.compile(r"\s*\d{1,3}(?!\d)")


def has_dot_leader(text: str) -> bool:
    """文本里是否出现目录式点线引导符。"""
    return bool(text) and _DOT_LEADER_RE.search(text) is not None


def strip_dot_leaders(text: str) -> str:
    """去掉目录条目的点线引导符，保留标题文字与页码。"""
    if not text:
        return text
    total = len(text)

    def repl(m: re.Match) -> str:
        span = m.group(0)
        tail = text[m.end():]
        # 目录引导符的判据：后面紧跟页码、后面只剩页码，或在文本里占比很大。
        # 长句子中间零星出现的 ". . . ." 更像数学省略号（\ldots），保留原样。
        if (
            _DOT_TAIL_RE.fullmatch(tail)
            or _DOT_PAGE_RE.match(tail)
            or len(span) / total >= 0.25
        ):
            return " "
        return span

    out = _DOT_LEADER_RE.sub(repl, text)
    return _SPACE_RUN_RE.sub(" ", out).strip()


def is_dot_leader_only(text: str) -> bool:
    """整段是否只有点线（可能夹页码/括号），没有任何可读正文。"""
    t = (text or "").strip()
    if not t:
        return True
    if len(_DOT_CHAR_RE.findall(t)) < 4:
        return False
    return len(_DOT_ONLY_CORE_RE.sub("", t)) <= 2


def squeeze_cjk_spaces(text: str) -> tuple[str, list[int]]:
    """删除 CJK 之间多余的空白。

    部分中文 PDF（扫描件 OCR 文本层、部分排版工具导出）会把每个汉字写成
    独立的带间距文本，PyMuPDF 原样保留后在阅读页里就成了「中 文 文 献」。

    Returns:
        (清理后的文本, 旧文本下标 → 新文本下标的映射)
    """
    matches = list(_CJK_GAP_RE.finditer(text))
    if not matches:
        return text, list(range(len(text) + 1))
    drop = bytearray(len(text))
    for m in matches:
        drop[m.start():m.end()] = b"\x01" * (m.end() - m.start())
    out: list[str] = []
    mapping = [0] * (len(text) + 1)
    for i, ch in enumerate(text):
        mapping[i] = len(out)
        if not drop[i]:
            out.append(ch)
    mapping[len(text)] = len(out)
    return "".join(out), mapping