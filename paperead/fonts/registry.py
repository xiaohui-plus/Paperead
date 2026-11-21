"""字体语义注册表：把 PDF 字体名归一化并解析成稳定的角色。

PDF 字体名不能直接当身份：子集前缀（``ABCDEF+``）、样式后缀
（``-Regular``/``-Italic``）、截断名（``STIXMathCalligraphy-Regu``）会让
同一个字体出现多种写法。解析器若继续按原始名做 ``startswith`` 白名单，
每遇到一本新期刊就要再加一条规则。

这里把字体身份收敛为 :class:`FontFace`：family（归一化家族名）+ style +
role（锚点 / 符号 / 辅助 / 正文 / 中文 / 未知）。判定只读角色，字体名与
解码器细节都留在本模块。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache


class FontRole(str, Enum):
    ANCHOR = "anchor"      # 可单独开启行内公式组
    SYMBOL = "symbol"      # 符号字体，需要上下文证据才能开组
    AUX = "aux"            # 只能并入已有公式组的短片段
    TEXT = "text"          # 正文衬线/无衬线
    CJK = "cjk"            # 中文/日韩，永不作数学
    UNKNOWN = "unknown"    # 未注册，交给码点证据与兜底


@dataclass(frozen=True)
class FontFace:
    raw: str
    family: str
    style: str
    role: FontRole
    algebra: str
    decoder: str

    def to_dict(self) -> dict:
        return {
            "raw": self.raw,
            "family": self.family,
            "style": self.style,
            "role": self.role.value,
            "algebra": self.algebra,
            "decoder": self.decoder,
        }


# 历史判定口径。这些前缀按原始字体名匹配，保证注册表接入不改变既有结果。
LEGACY_ANCHOR_PREFIXES = (
    "CMMI", "CMSY", "CMEX", "MSBM", "MTMI", "MTEX", "EUSM", "EUFM",
    "Symbol", "STIXMath",
)
LEGACY_AUX_PREFIXES = ("CMR",)

# 归一化家族名前缀。命中顺序：ANCHOR → TEXT → AUX → CJK → UNKNOWN。
# 注意 TEXT 里的 ``texgyretermes`` 不能排在 ``texgyretermesmath`` 前面，
# 否则正文族会把数学族一起吞掉；顺序由 resolve() 固定。
ANCHOR_FAMILIES: tuple[str, ...] = (
    # Computer Modern / AMS（同时覆盖带子集前缀的写法）
    "cmmi", "cmsy", "cmex", "cmmib", "cmbsy", "msam", "msbm",
    # MathTime
    "mtmi", "mtex", "mtsy",
    # STIX 数学与符号
    "stixmath", "stixsize", "stixnonunicode", "stixvariants",
    # Latin Modern Math
    "latinmodernmath", "lmmath",
    # Word / Office 导出
    "cambriamath",
    # NewTX / Times 数学
    "newtxmi", "newtxsy", "rmtmi", "rmtsy",
    # pxfonts
    "pxsy", "pxbsy", "pxmi", "pxex",
    # Elsevier / Minion Math
    "esstix", "advminionmath", "advminionsymbols", "minionmath",
    # TeX Gyre Math（只收 *Math，正文 Termes/Pagella 见 TEXT）
    "texgyretermesmath", "texgyrepagellamath", "texgyrebonummath",
    "texgyrescholamath", "texgyreherosmath",
    # 其它常见数学族
    "xitsmath", "asanamath", "libertinusmath", "firamath",
    "notosansmath", "notoserifmath", "dejavumath", "lucidamath",
    "lucidabrightmath",
    # 纯符号字体
    "symbol", "symbolmt", "euclid",
)

# 正文族：即使名字里带 stix/texgyre 痕迹，也永远不能单独开启公式组。
# CMR 不列在这里：它在下方走 AUX，只在已开启的公式组内并入短片段。
TEXT_FAMILIES: tuple[str, ...] = (
    "stixgeneral",
    "texgyretermesx", "texgyretermes", "texgyrepagella",
    "texgyrebonum", "texgyreschola", "texgyreheros",
    "lmroman", "nimbusrom", "timesnewroman", "minionpro",
    "charis", "arial", "helvetica", "calibri", "dejavusans",
    "urwpalladio", "warnockpro", "myriadpro", "gillsans",
    "caecilialt", "hardingtext", "graphik", "linlibertine",
)

# 中文/日韩与中文字体子集名：永不作数学证据。
CJK_FAMILIES: tuple[str, ...] = (
    "advot", "advtt", "fjnsjk", "cmxhjx",
    "song", "hei", "kai", "ming", "fang",
    "simsun", "simhei", "msyh", "notosanscjk", "sourcehan",
)

_SUBSET_RE = re.compile(r"^[A-Z]{6}\+")
_STYLE_RE = re.compile(
    r"(?:^|[-_ ])(regular|regu|roman|italic|ital|bold|bd|light|lt|medium|"
    r"book|oblique|semibold|black|identit|identi)$",
    re.IGNORECASE,
)


def normalize_font(raw: str) -> tuple[str, str]:
    """返回 ``(family, style)``：去子集前缀、样式后缀与分隔符。"""
    name = _SUBSET_RE.sub("", raw or "").strip()
    style = ""
    match = _STYLE_RE.search(name)
    if match:
        style = match.group(1).lower()
        name = name[: match.start()]
    family = re.sub(r"[^a-z0-9]+", "", name.lower())
    if not family:
        family = re.sub(r"[^a-z0-9]+", "", (raw or "").lower())
    return family, style


def _algebra_for(family: str) -> str:
    if family.startswith(
        ("cmmi", "cmsy", "cmex", "cmmib", "cmbsy", "msam", "msbm")
    ):
        return "cm"
    if family.startswith(("mtmi", "mtex", "mtsy")):
        return "mathtime"
    if family.startswith("stix"):
        return "stix"
    if family.startswith(("lmmath", "latinmodernmath")):
        return "lm"
    if family.startswith("cambriamath"):
        return "cambria"
    if family.startswith(("newtxmi", "newtxsy", "rmtmi", "rmtsy")):
        return "newtx"
    if family.startswith(("pxsy", "pxbsy", "pxmi", "pxex")):
        return "px"
    if family.startswith("esstix"):
        return "esstix"
    if family.startswith(("advminionmath", "advminionsymbols", "minionmath")):
        return "minion"
    if family.startswith("texgyre"):
        return "texgyre"
    if family.startswith("xitsmath"):
        return "xits"
    if family.startswith("asanamath"):
        return "asana"
    if family.startswith("libertinusmath"):
        return "libertinus"
    if family.startswith("firamath"):
        return "fira"
    if family.startswith(("notosansmath", "notoserifmath")):
        return "noto"
    if family.startswith("dejavumath"):
        return "dejavu"
    if family.startswith(("lucidamath", "lucidabrightmath")):
        return "lucida"
    if family.startswith(("symbol", "euclid")):
        return "symbol"
    return ""


def _decoder_for(algebra: str) -> str:
    if algebra == "stix":
        return "stix_pua"
    if algebra in ("cm", "mathtime", "symbol"):
        return "cm_legacy"
    if algebra:
        return "unicode_nfkc"
    return "none"


def _starts_with_any(value: str, prefixes: tuple[str, ...]) -> str | None:
    for prefix in prefixes:
        if value.startswith(prefix):
            return prefix
    return None


@lru_cache(maxsize=1024)
def resolve_font(raw: str) -> FontFace:
    """字体名 → 角色。结果缓存；字体种类有限，解析阶段开销可忽略。"""
    family, style = normalize_font(raw)

    if _starts_with_any(raw or "", LEGACY_ANCHOR_PREFIXES):
        algebra = _algebra_for(family) or "cm"
        return FontFace(raw, family, style, FontRole.ANCHOR, algebra,
                        _decoder_for(algebra))
    if _starts_with_any(raw or "", LEGACY_AUX_PREFIXES):
        return FontFace(raw, family, style, FontRole.AUX, "cm", "cm_legacy")

    if _starts_with_any(family, ANCHOR_FAMILIES):
        algebra = _algebra_for(family)
        return FontFace(raw, family, style, FontRole.ANCHOR, algebra,
                        _decoder_for(algebra))
    if _starts_with_any(family, TEXT_FAMILIES):
        return FontFace(raw, family, style, FontRole.TEXT, "", "none")
    if family.startswith("cmr"):
        return FontFace(raw, family, style, FontRole.AUX, "cm", "cm_legacy")
    if _starts_with_any(family, CJK_FAMILIES):
        return FontFace(raw, family, style, FontRole.CJK, "", "none")
    return FontFace(raw, family, style, FontRole.UNKNOWN, "", "none")


def is_anchor_font(raw: str) -> bool:
    return resolve_font(raw).role is FontRole.ANCHOR


def is_aux_font(raw: str) -> bool:
    return resolve_font(raw).role is FontRole.AUX


def is_math_font(raw: str) -> bool:
    return resolve_font(raw).role in (
        FontRole.ANCHOR, FontRole.SYMBOL, FontRole.AUX,
    )


def anchor_prefixes() -> tuple[str, ...]:
    """给回归报告/兼容层用的锚点字体前缀（原始名 + 归一化家族名）。"""
    return LEGACY_ANCHOR_PREFIXES + ANCHOR_FAMILIES