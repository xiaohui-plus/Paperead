import type { PaperMetadata } from './types'

/**
 * 在页面上下文执行。必须保持自包含：不能引用模块级函数或 import，
 * 因为 chrome.scripting.executeScript 只会序列化函数体。
 */
export function extractMetadataInPage(href = location.href): PaperMetadata {
  const meta = (name: string) => {
    const element = document.querySelector(
      `meta[name="${name}"], meta[property="${name}"]`,
    ) as HTMLMetaElement | null
    return (element?.content || '').trim()
  }

  const metas = (name: string) =>
    Array.from(
      document.querySelectorAll(`meta[name="${name}"]`) as NodeListOf<HTMLMetaElement>,
    )
      .map((element) => (element.content || '').trim())
      .filter(Boolean)

  let ld: Record<string, unknown> | null = null
  for (const script of Array.from(
    document.querySelectorAll('script[type="application/ld+json"]'),
  )) {
    try {
      const parsed = JSON.parse(script.textContent || '')
      const items = Array.isArray(parsed) ? parsed : [parsed]
      const hit = items.find((item) =>
        String((item as Record<string, unknown>)?.['@type'] || '').match(
          /ScholarlyArticle|Article/,
        ),
      )
      if (hit) ld = hit as Record<string, unknown>
    } catch {
      // 单个 JSON-LD 损坏不影响其它元数据
    }
  }

  const html = document.documentElement.innerHTML

  const arxiv =
    href.match(/arxiv\.org\/(?:abs|pdf|html)\/(\d{4}\.\d{4,5})(?:v\d+)?/i) ||
    href.match(
      /ar5iv(?:\.labs\.arxiv\.org|labs\.arxiv\.org|\.org)\/(?:abs|html)\/(\d{4}\.\d{4,5})(?:v\d+)?/i,
    )
  const arxivId = arxiv ? arxiv[1] : ''
  const openreviewId = /openreview\.net/i.test(href)
    ? new URL(href, location.href).searchParams.get('id') || ''
    : ''
  const aclId =
    (href.match(/aclanthology\.org\/([A-Za-z0-9._-]+)\/?$/i) || [])[1] || ''
  const pubmed = href.match(/pubmed\.ncbi\.nlm\.nih\.gov\/(\d+)/i)
  const pmc = href.match(/pmc\.ncbi\.nlm\.nih\.gov\/articles\/(PMC\d+)/i)

  const doiMeta = meta('citation_doi')
  const doiMatch = (doiMeta || href || html).match(/10\.\d{4,9}\/[^\s"'<>]+/)
  const doi = (doiMeta || (doiMatch ? doiMatch[0] : '')).replace(/[.,;)\]]+$/, '')

  let source = 'web'
  let sourceId = ''
  if (arxivId) {
    source = 'arxiv'
    sourceId = `arxiv:${arxivId}`
  } else if (openreviewId) {
    source = 'openreview'
    sourceId = `openreview:${openreviewId}`
  } else if (aclId) {
    source = 'acl'
    sourceId = `aclanthology:${aclId}`
  } else if (pubmed) {
    source = 'pubmed'
    sourceId = `pubmed:${pubmed[1]}`
  } else if (pmc) {
    source = 'pmc'
    sourceId = `pmc:${pmc[1]!.toUpperCase()}`
  } else if (doi) {
    source = 'doi'
    sourceId = `doi:${doi.toLowerCase()}`
  }

  const authors =
    metas('citation_author').join(', ') ||
    (() => {
      const author = ld?.author
      if (Array.isArray(author)) {
        return author
          .map((item) =>
            typeof item === 'string'
              ? item
              : String((item as Record<string, unknown>)?.name || ''),
          )
          .filter(Boolean)
          .join(', ')
      }
      if (typeof author === 'string') return author
      if (author && typeof author === 'object') {
        return String((author as Record<string, unknown>).name || '')
      }
      return ''
    })()

  const date =
    meta('citation_publication_date') ||
    (typeof ld?.datePublished === 'string' ? ld.datePublished : '')
  const yearMatch = date.match(/(19|20)\d{2}/)
  const year = yearMatch ? Number(yearMatch[0]) : null

  const pdfLink = document.querySelector(
    'link[type="application/pdf"]',
  ) as HTMLLinkElement | null
  const anchor = Array.from(document.querySelectorAll('a[href]')).find((item) =>
    /\.pdf($|[?#])/i.test((item as HTMLAnchorElement).href || ''),
  ) as HTMLAnchorElement | null

  return {
    source,
    source_id: sourceId,
    title:
      meta('citation_title') ||
      (typeof ld?.headline === 'string' ? ld.headline : '') ||
      meta('og:title') ||
      document.title ||
      'Untitled paper',
    authors,
    abstract:
      meta('citation_abstract') ||
      (typeof ld?.description === 'string' ? ld.description : '') ||
      meta('og:description'),
    doi,
    url: href,
    year,
    venue:
      meta('citation_journal_title') ||
      meta('citation_conference_title') ||
      (() => {
        const part = ld?.isPartOf
        return part && typeof part === 'object'
          ? String((part as Record<string, unknown>).name || '')
          : ''
      })(),
    pdf_url:
      meta('citation_pdf_url') ||
      pdfLink?.href ||
      (arxivId ? `https://arxiv.org/pdf/${arxivId}` : '') ||
      (openreviewId ? `https://openreview.net/pdf?id=${openreviewId}` : '') ||
      (aclId ? `https://aclanthology.org/${aclId}.pdf` : '') ||
      anchor?.href ||
      '',
  }
}
