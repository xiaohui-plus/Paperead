import { beforeEach, describe, expect, it } from 'vitest'
import { extractMetadataInPage } from './metadata'

function render(head: string) {
  document.head.innerHTML = head
  document.body.innerHTML = ''
}

describe('extractMetadataInPage', () => {
  beforeEach(() => {
    document.head.innerHTML = ''
    document.body.innerHTML = ''
  })

  it('reads arXiv citation metadata', () => {
    render(`
      <meta name="citation_title" content="A Test Paper" />
      <meta name="citation_author" content="Ada Lovelace" />
      <meta name="citation_author" content="Alan Turing" />
      <meta name="citation_publication_date" content="2024-01-02" />
      <meta name="citation_doi" content="10.1000/test" />
      <meta name="citation_pdf_url" content="https://arxiv.org/pdf/2401.00001" />
      <meta name="citation_abstract" content="Testing ingestion." />
    `)

    const meta = extractMetadataInPage('https://arxiv.org/abs/2401.00001')
    expect(meta.title).toBe('A Test Paper')
    expect(meta.authors).toBe('Ada Lovelace, Alan Turing')
    expect(meta.year).toBe(2024)
    expect(meta.doi).toBe('10.1000/test')
    expect(meta.source).toBe('arxiv')
    expect(meta.source_id).toBe('arxiv:2401.00001')
    expect(meta.pdf_url).toBe('https://arxiv.org/pdf/2401.00001')
  })

  it('derives arXiv pdf url from abs url', () => {
    render('')

    const meta = extractMetadataInPage('https://arxiv.org/abs/2401.00001v2')
    expect(meta.source_id).toBe('arxiv:2401.00001')
    expect(meta.pdf_url).toBe('https://arxiv.org/pdf/2401.00001')
  })

  it('reads JSON-LD scholarly article', () => {
    render(`
      <script type="application/ld+json">
        {
          "@type": "ScholarlyArticle",
          "headline": "JSON Paper",
          "author": [{ "name": "Grace Hopper" }],
          "datePublished": "2023-06-01",
          "isPartOf": { "name": "Journal of Tests" },
          "description": "From JSON-LD."
        }
      </script>
    `)

    const meta = extractMetadataInPage('https://example.com/article/123')
    expect(meta.title).toBe('JSON Paper')
    expect(meta.authors).toBe('Grace Hopper')
    expect(meta.year).toBe(2023)
    expect(meta.venue).toBe('Journal of Tests')
    expect(meta.abstract).toBe('From JSON-LD.')
  })

  it('detects PubMed id', () => {
    render('')
    const meta = extractMetadataInPage('https://pubmed.ncbi.nlm.nih.gov/12345678/')
    expect(meta.source).toBe('pubmed')
    expect(meta.source_id).toBe('pubmed:12345678')
  })
})
