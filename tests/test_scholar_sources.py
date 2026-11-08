"""数据源解析与查询构造的回归测试（不联网）。"""

from __future__ import annotations

from urllib.parse import unquote

import pytest

from paper_reflow.scholar.models import (
    Paper,
    clean_html_text,
    decode_xml,
    epmc_key,
    epmc_pdf_url,
    reconstruct_abstract,
    split_epmc_key,
    strip_doi,
    strip_latex_pointers,
)
from paper_reflow.scholar.sources import (
    build_arxiv_query,
    encode_phrase,
    europe_pmc_url,
    parse_arxiv_entries,
    parse_europe_pmc,
    parse_openalex_works,
    parse_pubmed_abstract_xml,
    parse_pubmed_summary,
)

ARXIV_XML = r"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <opensearch:totalResults>2</opensearch:totalResults>
  <entry>
    <id>http://arxiv.org/abs/2401.00001v2</id>
    <published>2024-01-02T10:00:00Z</published>
    <title>Graph   Neural \cite{KL} Networks</title>
    <summary>We study $x$ &amp; more, see \ref{eq:1}.</summary>
    <author><name>John Smith</name></author>
    <author><name>Jane Doe</name></author>
    <link title="pdf" href="http://arxiv.org/pdf/2401.00001v2"/>
    <arxiv:primary_category term="cs.LG"/>
    <category term="cs.LG"/>
    <category term="stat.ML"/>
    <arxiv:doi>10.1000/abc</arxiv:doi>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2401.00002v1</id>
    <published>2024-02-03T00:00:00Z</published>
    <title>Second Paper</title>
    <summary>Nothing here.</summary>
    <author><name>Only Author</name></author>
    <category term="math.AG"/>
  </entry>
</feed>
"""


def test_decode_xml_strips_tags_cdata_and_entities():
    raw = "<![CDATA[<p>A &amp; B</p>]]> &lt;tag&gt;"
    assert decode_xml(raw) == "A & B <tag>"


def test_strip_latex_pointers_removes_only_pointer_macros():
    assert strip_latex_pointers(r"见 \cite{KL} 与 \ref{eq:1} 的结果") == "见 与 的结果"
    # 数学宏与普通文本不动
    assert strip_latex_pointers(r"$\sigma$ 不大") == r"$\sigma$ 不大"
    assert strip_latex_pointers("plain text") == "plain text"


def test_clean_html_text_turns_section_headings_into_lines():
    raw = "<h4>Background</h4><p>We did X.</p><h4>Results</h4>Y &amp; Z."
    assert clean_html_text(raw) == "Background\nWe did X.\nResults\nY & Z."


def test_reconstruct_abstract_orders_words_by_position():
    inverted = {"world": [1], "Hello": [0], "again": [2]}
    assert reconstruct_abstract(inverted) == "Hello world again"
    assert reconstruct_abstract(None) == ""


def test_epmc_key_roundtrip_and_pdf_pick():
    key = epmc_key("PMC", "PMC4744125")
    assert key == "europepmc:PMC/PMC4744125"
    assert split_epmc_key("PMC/PMC4744125") == ("PMC", "PMC4744125")
    assert split_epmc_key("broken") is None

    record = {
        "fullTextUrlList": {
            "fullTextUrl": [
                {"availabilityCode": "OA", "documentStyle": "html", "url": "https://x/html"},
                {"availabilityCode": "OA", "documentStyle": "pdf", "url": "https://x/pdf"},
                {"availabilityCode": "S", "documentStyle": "pdf", "url": "https://x/sub"},
            ]
        }
    }
    assert epmc_pdf_url(record) == "https://x/pdf"


def test_strip_doi_normalizes_url_forms():
    assert strip_doi("https://doi.org/10.1/abc") == "10.1/abc"
    assert strip_doi("http://dx.doi.org/10.1/abc") == "10.1/abc"
    assert strip_doi(None) is None


def test_encode_phrase_uses_plus_and_quotes():
    assert encode_phrase("graph neural network") == "%22graph+neural+network%22"


def test_build_arxiv_query_fields_phrase_category_and_year():
    query = build_arxiv_query(
        "graph neural network",
        field="title",
        category="cs.LG",
        from_year=2015,
        to_year=2020,
    )
    assert query == (
        "ti:graph+AND+ti:neural+AND+ti:network+AND+cat:cs.LG+AND+"
        "submittedDate:[201501010000+TO+202012312359]"
    )

    phrase = build_arxiv_query("graph neural", field="abstract", phrase=True)
    assert phrase == "abs:%22graph+neural%22"


def test_parse_arxiv_entries_extracts_metadata():
    papers = parse_arxiv_entries(ARXIV_XML)
    assert len(papers) == 2

    first = papers[0]
    assert first.id == "arxiv:2401.00001"
    assert first.title == "Graph Neural Networks"
    assert first.abstract == "We study $x$ & more, see."
    assert first.authors == ["John Smith", "Jane Doe"]
    assert first.category == "cs.LG"
    assert first.keywords == ["cs.LG", "stat.ML"]
    assert first.year == 2024
    assert first.doi == "10.1000/abc"
    assert first.pdf_url == "http://arxiv.org/pdf/2401.00001v2"
    assert first.venue == "arXiv 预印本"
    assert first.html_url == "https://arxiv.org/html/2401.00001"

    second = papers[1]
    assert second.id == "arxiv:2401.00002"
    assert second.category == "math.AG"
    assert second.pdf_url == "https://arxiv.org/pdf/2401.00002"
    assert second.doi is None


def test_parse_arxiv_entries_tolerates_garbage():
    assert parse_arxiv_entries("") == []
    assert parse_arxiv_entries("<not-xml") == []


OPENALEX_PAYLOAD = {
    "meta": {"count": 7},
    "results": [
        {
            "id": "https://openalex.org/W123",
            "doi": "https://doi.org/10.1/x",
            "title": "A Work",
            "publication_year": 2021,
            "publication_date": "2021-05-01",
            "cited_by_count": 42,
            "abstract_inverted_index": {"Hello": [0], "world": [1]},
            "authorships": [
                {"author": {"display_name": "Ann Lee"}},
                {"author": {"display_name": "Bob Chen"}},
            ],
            "primary_location": {
                "landing_page_url": "https://example.org/w",
                "source": {"display_name": "Nature"},
            },
            "primary_topic": {"field": {"display_name": "Computer Science"}},
            "keywords": [{"display_name": "kw1"}, {"display_name": "kw2"}],
            "best_oa_location": {"pdf_url": "https://example.org/w.pdf"},
        }
    ],
}


def test_parse_openalex_works_reconstructs_abstract_and_links():
    papers = parse_openalex_works(OPENALEX_PAYLOAD)
    assert len(papers) == 1
    paper = papers[0]
    assert paper.id == "openalex:W123"
    assert paper.doi == "10.1/x"
    assert paper.abstract == "Hello world"
    assert paper.authors == ["Ann Lee", "Bob Chen"]
    assert paper.venue == "Nature"
    assert paper.category == "Computer Science"
    assert paper.pdf_url == "https://example.org/w.pdf"
    assert paper.citations == 42
    assert paper.url == "https://example.org/w"


def test_parse_openalex_falls_back_to_openalex_url():
    payload = {
        "results": [
            {
                "id": "https://openalex.org/W9",
                "title": "No Links",
                "primary_location": None,
                "authorships": None,
            }
        ]
    }
    paper = parse_openalex_works(payload)[0]
    assert paper.url == "https://openalex.org/W9"
    assert paper.pdf_url is None
    assert paper.authors == []


EPMC_PAYLOAD = {
    "hitCount": 3,
    "resultList": {
        "result": [
            {
                "id": "PMC4744125",
                "source": "PMC",
                "title": "A Biomedical Paper",
                "authorString": "Lee A, Chen B.",
                "pubYear": "2016",
                "firstPublicationDate": "2016-01-01",
                "abstractText": "<h4>Background</h4>We did X.",
                "citedByCount": 9,
                "journalInfo": {"journal": {"title": "J Test"}},
                "pubTypeList": {"pubType": ["review"]},
                "keywordList": {"keyword": ["crispr"]},
                "meshHeadingList": {"meshHeading": [{"descriptorName": "Genetics"}]},
                "fullTextUrlList": {
                    "fullTextUrl": [
                        {"availabilityCode": "OA", "documentStyle": "pdf", "url": "https://x/pdf"}
                    ]
                },
            }
        ]
    },
}


def test_parse_europe_pmc_maps_fields():
    papers = parse_europe_pmc(EPMC_PAYLOAD)
    assert len(papers) == 1
    paper = papers[0]
    assert paper.id == "europepmc:PMC/PMC4744125"
    assert paper.source == "europepmc"
    assert paper.authors == ["Lee A", "Chen B."]
    assert paper.abstract == "Background\nWe did X."
    assert paper.category == "Genetics"
    assert paper.pdf_url == "https://x/pdf"
    assert paper.citations == 9
    assert paper.year == 2016
    assert paper.url == "https://europepmc.org/article/PMC/PMC4744125"


def test_europe_pmc_url_page_limit_and_window():
    url, page_limit = europe_pmc_url(
        "crispr", field="all", phrase=False, from_year=None, to_year=None,
        sort="rel", page=9, per_page=20,
    )
    # 200 条窗口 / 每页 20 → 最多 10 页
    assert page_limit == 10
    decoded = unquote(url)
    assert "pageSize=200" in decoded
    assert "query=crispr" in decoded


def test_europe_pmc_url_adds_year_range_and_sort():
    url, _ = europe_pmc_url(
        "crispr", field="title", phrase=True, from_year=2010, to_year=2020,
        sort="new", page=0, per_page=10,
    )
    decoded = unquote(url)
    assert 'TITLE:"crispr"' in decoded
    assert "FIRST_PDATE:[2010-01-01 TO 2020-12-31]" in decoded
    assert "sort=P_PDATE_D desc" in decoded


PUBMED_SUMMARY = {
    "uid": "26780180",
    "title": "A PubMed Paper",
    "fulljournalname": "Journal of Tests",
    "pubdate": "2016 Mar",
    "authors": [{"name": "Lee A"}, {"name": "Chen B"}],
    "articleids": [
        {"idtype": "doi", "value": "10.1/pm"},
        {"idtype": "pmc", "value": "PMC4744125"},
    ],
    "pubtype": ["Journal Article"],
}

PUBMED_XML = """<PubmedArticle>
  <MedlineCitation>
    <Article>
      <Abstract>
        <AbstractText Label="BACKGROUND">We did X.</AbstractText>
        <AbstractText Label="RESULTS">We found Y.</AbstractText>
      </Abstract>
      <KeywordList><Keyword>crispr</Keyword></KeywordList>
    </Article>
  </MedlineCitation>
</PubmedArticle>"""


def test_parse_pubmed_summary_links_and_ids():
    paper = parse_pubmed_summary(PUBMED_SUMMARY)
    assert paper.id == "pubmed:26780180"
    assert paper.doi == "10.1/pm"
    # PMC landing page 不是 PDF，不能当全文直链用
    assert paper.pdf_url is None
    assert paper.year == 2016
    assert paper.category == "Journal Article"
    assert paper.venue == "Journal of Tests"


def test_parse_pubmed_abstract_xml_sections_and_keywords():
    abstract, keywords = parse_pubmed_abstract_xml(PUBMED_XML)
    assert abstract == "BACKGROUND: We did X.\n\nRESULTS: We found Y."
    assert keywords == ["crispr"]


def test_paper_to_dict_is_api_shape():
    paper = Paper(id="arxiv:1", source="arxiv", title="T")
    data = paper.to_dict()
    assert data["pdf_url"] is None
    assert data["authors"] == []
    assert set(data) == {
        "id", "source", "title", "authors", "venue", "year", "category",
        "keywords", "abstract", "url", "pdf_url", "html_url", "published_at",
        "doi", "citations",
    }


@pytest.mark.parametrize("value", [None, "", "not-a-year-99999"])
def test_normalize_year_rejects_bad_values(value):
    from paper_reflow.scholar.models import normalize_year

    assert normalize_year(value) is None
