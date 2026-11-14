"""正文引用标记识别与参考文献映射的单元测试。

覆盖 `paperead/tutor/citations.py` 的三种引用写法、首作者 + 年份后缀打分、
已用条目去重，以及解析不到时「宁缺毋滥」的降级行为。
"""

from __future__ import annotations

from paperead.tutor.citations import (
    Citation,
    _ascii,
    _author_zone,
    _expand,
    _name_match,
    _ref_suffix,
    _score,
    find,
    find_paren_numeric,
    marker_key,
    match_index,
    resolve,
)
from paperead.tutor.references import Reference


def make_ref(
    index: int = 1,
    raw: str = "",
    title: str = "A title",
    year: int | None = 2016,
    authors: list[str] | None = None,
    suffix: str = "",
) -> Reference:
    return Reference(
        index=index,
        raw=raw or f"Ref {index} {year or ''}",
        title=title,
        year=year,
        authors=list(authors or []),
        suffix=suffix,
    )


def make_cite(**overrides) -> Citation:
    base = dict(
        marker="",
        index=None,
        surname=None,
        year=2016,
        start=0,
        end=0,
        suffix="",
        authors=[],
    )
    base.update(overrides)
    return Citation(**base)


# ── 基础工具 ──────────────────────────────────────────


def test_expand_supports_lists_ranges_and_rejects_noise():
    assert _expand("3") == [3]
    assert _expand("3,5") == [3, 5]
    assert _expand("3-5") == [3, 4, 5]
    # 区间过大不展开，避免 [1-999] 把响应撑爆
    assert _expand("3-30") == [3, 30]
    # 起止相同不满足 0 < b - a，按两端处理
    assert _expand("5-5") == [5, 5]
    assert _expand("x,7") == [7]


def test_ascii_folds_diacritics_for_name_matching():
    assert _ascii("Veličković") == "velickovic"
    assert _ascii("Kipf") == "kipf"


def test_author_zone_stays_before_year_when_prefix_is_long():
    raw = "[1] Kipf, T. N., and Welling, M. 2016. Semi-supervised classification."
    zone = _author_zone(raw)
    assert "Kipf" in zone
    assert "Welling" in zone
    assert "2016" not in zone


def test_author_zone_falls_back_to_first_180_chars_when_year_is_early():
    raw = "12. 2016 proceedings of the conference on very large data bases and systems"
    zone = _author_zone(raw)
    assert zone.startswith("2016")
    assert "12." not in zone


def test_name_match_handles_exact_diacritic_and_empty_input():
    assert _name_match("Kipf", "[1] Kipf, T. 2016. Title")
    # 变音符号被 NFKD 折叠成 ASCII 后仍能精确匹配
    assert _name_match("Velickovic", "[1] Veličković, P. 2017. Title")
    # 只有前 5 个字符一致的轻微拼写差异也算命中
    assert _name_match("Velickovicz", "[1] Veličković, P. 2017. Title")
    assert not _name_match("", "[1] Kipf, T. 2016.")
    assert not _name_match("Smith", "12. 2001 something else entirely")


def test_ref_suffix_prefers_structured_field_then_falls_back_to_raw():
    assert _ref_suffix(make_ref(suffix="b", raw="Kipf and Welling 2016b"), 2016) == "b"
    assert _ref_suffix(make_ref(suffix="", raw="Kipf and Welling 2016b."), 2016) == "b"
    assert _ref_suffix(make_ref(suffix="", raw="Kipf and Welling 2016."), 2016) == ""


# ── 打分：作者与年份后缀 ───────────────────────────────


def test_score_rejects_year_and_suffix_mismatch():
    assert _score(make_cite(year=2017, authors=["Kipf"]), make_ref(year=2016, authors=["Kipf"])) == -1
    assert (
        _score(
            make_cite(suffix="a", authors=["Kipf"]),
            make_ref(year=2016, suffix="b", authors=["Kipf"]),
        )
        == -1
    )
    # 参考文献没有年份时直接排除
    assert _score(make_cite(authors=["Kipf"]), make_ref(year=None, authors=["Kipf"])) == -1


def test_score_first_author_and_suffix_hits():
    assert (
        _score(
            make_cite(suffix="a", authors=["Kipf", "Welling"]),
            make_ref(year=2016, suffix="a", authors=["Kipf", "Welling"]),
        )
        == 10
    )
    # 正文没写后缀时优先无后缀 / a 的那篇
    assert _score(make_cite(authors=["Kipf"]), make_ref(year=2016, suffix="", authors=["Kipf"])) == 7
    # b 后缀那篇在正文无后缀时降权
    assert _score(make_cite(authors=["Kipf"]), make_ref(year=2016, suffix="b", authors=["Kipf"])) == 6


def test_score_accepts_full_author_set_even_when_first_differs():
    ref = make_ref(year=2016, authors=["Louis", "Jacob", "Salehi-Abari"])
    cite = make_cite(authors=["Jacob", "Louis"])
    # 首作者不同，但作者集合完整命中：基础 1 分 + 3 分
    assert _score(cite, ref) == 4


def test_score_falls_back_to_literal_match_with_thin_author_data():
    ref = make_ref(
        raw="Kipf, T. and Welling, M. 2016. Title",
        year=2016,
        authors=["Kipf"],
    )
    # 参考文献只解析出一个作者：退回字面匹配（+2）
    assert _score(make_cite(surname="Welling", authors=["Welling", "Kipf"]), ref) == 3
    # 作者区完全没解析出来：同样退回字面匹配
    loose = make_ref(raw="Kipf, T. and Welling, M. 2016. Title", year=2016, authors=[])
    assert _score(make_cite(authors=["Kipf"]), loose) == 3
    # 字面也匹配不上就排除
    assert _score(make_cite(authors=["Smith"]), ref) == -1


def test_score_without_parsed_authors_uses_surname_literal():
    ref = make_ref(raw="Kipf, T. and Welling, M. 2016. Title", year=2016, authors=[])
    assert _score(make_cite(surname="Kipf", authors=[]), ref) == 3
    assert _score(make_cite(surname="Nobody", authors=[]), ref) == -1
    # 正文无后缀 + 文献带 b 后缀：基础 0 + 2 = 2，低于阈值 3
    suffixed = make_ref(raw="Kipf, T. 2016b. Title", year=2016, authors=[], suffix="b")
    assert _score(make_cite(surname="Kipf", authors=[]), suffixed) == 2


# ── 正文标记识别 ──────────────────────────────────────


def test_find_numeric_markers_expands_ranges():
    text = "Prior work [3-5] and [7] and [9,11] agrees."
    cites = find(text)
    assert [c.index for c in cites] == [3, 4, 5, 7, 9, 11]
    for c in cites:
        assert text[c.start : c.end] == c.marker


def test_find_author_year_variants_and_multi_author():
    text = (
        "(Kipf and Welling 2016a) then Zhang et al. 2018 "
        "and (Louis, Jacob, and Salehi-Abari 2023)."
    )
    cites = find(text)
    assert len(cites) == 3
    assert [(c.surname, c.year, c.suffix) for c in cites] == [
        ("Kipf", 2016, "a"),
        ("Zhang", 2018, ""),
        ("Louis", 2023, ""),
    ]
    assert cites[0].authors == ["Kipf", "Welling"]
    assert cites[1].authors == ["Zhang"]
    assert cites[2].authors == ["Louis", "Jacob", "Salehi-Abari"]
    assert cites[0].marker == "Kipf and Welling 2016a"
    assert cites[1].marker == "Zhang et al. 2018"


def test_find_ignores_stopword_authors_and_bare_years():
    assert find("In 2019 we measured accuracy.") == []
    assert find("Table 2020 lists the results.") == []
    assert find("2020 was a productive year for the lab.") == []
    assert find("the results were published in 2021 by others.") == []


def test_find_paren_numeric_parses_groups():
    cites = find_paren_numeric("As shown (1,2) and (8) and (10-12).")
    assert [c.index for c in cites] == [1, 2, 8, 10, 11, 12]
    assert all(c.marker.startswith("(") for c in cites)


# ── 落条与去重 ────────────────────────────────────────


def test_match_index_by_printed_number():
    refs = [make_ref(index=1), make_ref(index=2)]
    assert match_index(make_cite(index=2), refs) == 2
    assert match_index(make_cite(index=9), refs) is None


def test_match_index_honours_used_set_and_min_score():
    kipf = make_ref(index=1, raw="Kipf, T. 2016. Title", year=2016, authors=["Kipf"])
    smith = make_ref(index=2, raw="Smith, J. 2016. Other", year=2016, authors=["Smith"])
    cite = make_cite(authors=["Kipf"])
    assert match_index(cite, [smith, kipf]) == 1
    # 已被分配的条目不再给第二个引用
    assert match_index(cite, [smith, kipf], used={1}) is None
    # 恰好 2 分低于阈值 3，不应落条
    weak = make_ref(index=3, raw="Kipf, T. 2016b. Title", year=2016, authors=[], suffix="b")
    assert match_index(make_cite(surname="Kipf", authors=[]), [weak]) is None


def test_marker_key_normalizes_variants():
    assert marker_key("(Kipf & Welling 2016a)") == marker_key("Kipf and Welling 2016a")
    assert marker_key("（张 2020）") == "张 2020"
    assert marker_key("  A  B ") == "a b"


def test_resolve_dedupes_repeated_and_variant_markers():
    refs = [
        make_ref(
            index=1,
            raw="Kipf, T. and Welling, M. 2016a. Title",
            year=2016,
            authors=["Kipf", "Welling"],
            suffix="a",
        )
    ]
    cites = [
        make_cite(marker="Kipf and Welling 2016a", suffix="a", authors=["Kipf", "Welling"]),
        make_cite(marker="(Kipf & Welling 2016a)", suffix="a", authors=["Kipf", "Welling"]),
        make_cite(marker="(Kipf and Welling 2016a)", suffix="a", authors=["Kipf", "Welling"]),
    ]
    out = resolve(cites, refs)
    assert len(out) == 1
    assert out[0]["marker"] == "Kipf and Welling 2016a"
    assert out[0]["ref"]["index"] == 1


def test_resolve_assigns_same_year_suffixes_to_distinct_refs():
    refs = [
        make_ref(
            index=1,
            raw="Kipf, T. and Welling, M. 2016a. First",
            year=2016,
            authors=["Kipf", "Welling"],
            suffix="a",
        ),
        make_ref(
            index=2,
            raw="Kipf, T. and Welling, M. 2016b. Second",
            year=2016,
            authors=["Kipf", "Welling"],
            suffix="b",
        ),
    ]
    cites = [
        make_cite(marker="Kipf and Welling 2016a", suffix="a", authors=["Kipf", "Welling"]),
        make_cite(marker="Kipf and Welling 2016b", suffix="b", authors=["Kipf", "Welling"]),
    ]
    out = resolve(cites, refs)
    assert [item["ref"]["index"] for item in out] == [1, 2]


def test_resolve_does_not_reuse_one_reference_for_distinct_markers():
    refs = [
        make_ref(
            index=1,
            raw="Kipf, T. and Welling, M. 2016. Title",
            year=2016,
            authors=["Kipf", "Welling"],
        )
    ]
    cites = [
        make_cite(marker="Kipf and Welling 2016", authors=["Kipf", "Welling"]),
        make_cite(marker="Kipf 2016", authors=["Kipf"]),
    ]
    out = resolve(cites, refs)
    assert len(out) == 1
    assert out[0]["marker"] == "Kipf and Welling 2016"
    assert out[0]["ref"]["index"] == 1


def test_resolve_drops_unmatched_markers():
    refs = [make_ref(index=1, raw="Smith, J. 2019. Title", year=2019, authors=["Smith"])]
    cites = [make_cite(marker="Doe 2021", surname="Doe", year=2021, authors=["Doe"])]
    assert resolve(cites, refs) == []