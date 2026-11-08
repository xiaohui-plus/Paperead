"""引用格式回归测试。"""

from __future__ import annotations

from paperead.scholar.cite import (
    build_bibtex,
    build_ris,
    format_citation,
    split_name,
)
from paperead.scholar.models import Paper


def make_paper(**overrides) -> Paper:
    base = dict(
        id="arxiv:2401.00001",
        source="arxiv",
        title="A Study of Things",
        authors=["John Smith", "Jane Doe"],
        venue="arXiv 预印本",
        year=2024,
        category="cs.CL",
        keywords=["cs.CL"],
        abstract="We study things.",
        url="https://arxiv.org/abs/2401.00001",
        pdf_url="https://arxiv.org/pdf/2401.00001",
        html_url=None,
        published_at="2024-01-02T10:00:00Z",
        doi="10.1000/abc",
        citations=None,
    )
    base.update(overrides)
    return Paper(**base)


def test_split_name_handles_three_shapes():
    assert split_name("John Smith") == ("Smith", "John")
    assert split_name("Smith, John") == ("Smith", "John")
    assert split_name("Smith J") == ("Smith", "J")
    assert split_name("Jane A. Doe") == ("Doe", "Jane A.")
    assert split_name("") == ("", "")


def test_gb7714_takes_first_three_authors_and_doi():
    paper = make_paper(authors=["John Smith", "Jane Doe", "Bo Li", "Dan Wu"])
    text = format_citation(paper, "gb7714")
    assert text.startswith("SMITH J, DOE J, LI B, 等.")
    assert "A Study of Things[EB/OL]." in text
    assert text.endswith("doi:10.1000/abc.")


def test_gb7714_marks_journal_article():
    paper = make_paper(source="pubmed", venue="Nature", doi=None)
    text = format_citation(paper, "gb7714")
    assert "A Study of Things[J]." in text
    assert "Nature, 2024." in text


def test_apa_uses_ampersand_and_year():
    text = format_citation(make_paper(), "apa")
    assert text.startswith("Smith, J., & Doe, J.")
    assert "(2024)." in text
    assert text.endswith("https://doi.org/10.1000/abc")


def test_apa_without_year_uses_chinese_placeholder():
    text = format_citation(make_paper(year=None), "apa")
    assert "(无日期)." in text


def test_mla_quotes_title():
    text = format_citation(make_paper(), "mla")
    assert 'Smith, John, and Jane Doe.' in text
    assert '"A Study of Things."' in text
    assert "2024," in text


def test_ieee_puts_initials_first():
    text = format_citation(make_paper(), "ieee")
    assert 'J. Smith and J. Doe, "A Study of Things,"' in text
    assert "arXiv 预印本, 2024." in text


def test_bibtex_entry_fields():
    text = build_bibtex([make_paper()])
    assert text.startswith("@misc{Smith2024A,")
    assert "eprint       = {2401.00001}," in text
    assert "archivePrefix= {arXiv}," in text
    assert "primaryClass = {cs.CL}," in text
    assert "doi          = {10.1000/abc}," in text


def test_bibtex_escapes_and_journal_for_non_arxiv():
    paper = make_paper(
        source="openalex", venue="Journal of A & B", title="Costs $5_000", doi=None
    )
    text = build_bibtex([paper])
    assert text.startswith("@article{")
    assert r"journal      = {Journal of A \& B}," in text
    assert r"Costs \$5\_000" in text


def test_ris_record_layout():
    text = build_ris([make_paper()])
    assert text.startswith("TY  - GEN\nTI  - A Study of Things")
    assert "AU  - John Smith" in text
    assert "PY  - 2024" in text
    assert text.endswith("ER  - ")


def test_unknown_style_falls_back_to_gb7714():
    assert format_citation(make_paper(), "nope") == format_citation(make_paper(), "gb7714")
