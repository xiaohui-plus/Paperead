from paperead.fonts.registry import FontRole, is_math_font, normalize_font, resolve_font


def test_normalize_strips_subset_prefix_and_style_suffix():
    assert normalize_font("ABCDEF+LMMathItalic10-Regular") == (
        "lmmathitalic10", "regular",
    )
    assert normalize_font("STIXMathCalligraphy-Regu") == (
        "stixmathcalligraphy", "regu",
    )


def test_legacy_anchor_prefixes_keep_existing_behaviour():
    assert resolve_font("CMMI10").role is FontRole.ANCHOR
    assert resolve_font("CMSY10").role is FontRole.ANCHOR
    assert resolve_font("STIXMath-Italic").role is FontRole.ANCHOR
    assert resolve_font("SymbolMT").role is FontRole.ANCHOR


def test_cmr_is_aux_and_never_an_anchor():
    face = resolve_font("CMR10")
    assert face.role is FontRole.AUX
    assert is_math_font("CMR10") is True


def test_plain_serif_font_is_not_math():
    assert resolve_font("STIXGeneral-Regular").role is not FontRole.ANCHOR
    assert is_math_font("STIXGeneral-Regular") is False

def test_latin_modern_and_cambria_math_are_anchors():
    assert resolve_font("LMMathItalic10-Regular").role is FontRole.ANCHOR
    assert resolve_font("LMMathSymbols10-Regular").role is FontRole.ANCHOR
    assert resolve_font("CambriaMath").role is FontRole.ANCHOR


def test_subset_prefix_does_not_hide_math_family():
    face = resolve_font("ABCDEF+CambriaMath")
    assert face.family == "cambriamath"
    assert face.role is FontRole.ANCHOR


def test_text_serif_families_stay_text_even_with_math_like_names():
    assert resolve_font("STIXGeneral-Regular").role is FontRole.TEXT
    assert resolve_font("TeXGyreTermesX-Regular").role is FontRole.TEXT
    assert resolve_font("LMRoman10-Regular").role is FontRole.TEXT


def test_tex_gyre_math_and_text_are_distinguished():
    assert resolve_font("TeXGyreTermesMath-Regular").role is FontRole.ANCHOR
    assert resolve_font("TeXGyreTermesX-Italic").role is FontRole.TEXT


def test_cjk_subset_fonts_never_become_math():
    assert resolve_font("AdvOTdd63dae3").role is FontRole.CJK
    assert is_math_font("AdvOTdd63dae3") is False
