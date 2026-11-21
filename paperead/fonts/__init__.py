"""字体语义识别。"""

from .registry import (
    FontFace,
    FontRole,
    anchor_prefixes,
    is_anchor_font,
    is_aux_font,
    is_math_font,
    normalize_font,
    resolve_font,
)

__all__ = [
    "FontFace",
    "FontRole",
    "anchor_prefixes",
    "is_anchor_font",
    "is_aux_font",
    "is_math_font",
    "normalize_font",
    "resolve_font",
]