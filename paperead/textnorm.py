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