import { useEffect, useRef, useState } from 'react'
import { MathText } from './MathText'
import { IconBack, IconDownload, IconSpark } from './icons'
import {
  CITATION_STYLES,
  SOURCE_LABELS,
  citeScholarPaper,
  findFreeFullText,
  type CitationStyle,
  type FullTextResponse,
  type ScholarPaper,
} from '../lib/scholar'

interface Props {
  paper: ScholarPaper | null
  loading: boolean
  error: string
  onBack: () => void
  onImport: (paper: ScholarPaper) => void
  importing: boolean
}

/** 单篇文献详情：元信息、摘要、导入文献库、全文与引用。 */
export function ScholarDetail({ paper, loading, error, onBack, onImport, importing }: Props) {
  const [oa, setOa] = useState<FullTextResponse | null>(null)
  const [looking, setLooking] = useState(false)
  const [notice, setNotice] = useState('')
  const [copied, setCopied] = useState('')
  const citeRef = useRef<HTMLDetailsElement>(null)
  const copyTimer = useRef<number | null>(null)

  useEffect(() => {
    setOa(null)
    setNotice('')
    setCopied('')
  }, [paper?.id])

  useEffect(
    () => () => {
      if (copyTimer.current !== null) window.clearTimeout(copyTimer.current)
    },
    [],
  )

  const lookupFullText = async () => {
    if (!paper?.doi) return
    setLooking(true)
    setNotice('')
    try {
      const result = await findFreeFullText(paper.doi)
      if (result.error) setNotice(result.error)
      else if (!result.pdf_url && !result.landing_url) setNotice('未找到该论文的开放获取版本。')
      else {
        setOa(result)
        setNotice(result.pdf_url ? '已找到开放获取 PDF。' : '未找到可直接下载的 PDF，可打开开放获取页面。')
      }
    } catch (e) {
      setNotice(e instanceof Error ? e.message : '免费全文查询失败，请稍后重试。')
    } finally {
      setLooking(false)
    }
  }

  const copy = async (style: CitationStyle, label: string) => {
    if (!paper) return
    try {
      const text = await citeScholarPaper(paper, style)
      await navigator.clipboard.writeText(text)
      setNotice('')
      setCopied(label)
      citeRef.current?.removeAttribute('open')
      if (copyTimer.current !== null) window.clearTimeout(copyTimer.current)
      copyTimer.current = window.setTimeout(() => setCopied(''), 2000)
    } catch {
      setNotice('复制失败，请手动选择文本复制。')
    }
  }

  const pdfUrl = paper?.pdf_url ?? oa?.pdf_url ?? null

  return (
    <div className="scholar-detail">
      <button type="button" className="scholar-back" onClick={onBack}>
        <IconBack />
        返回检索结果
      </button>

      {loading && !paper && <p className="scholar-empty">正在加载文献详情…</p>}
      {!loading && error && !paper && <p className="scholar-empty">{error}</p>}

      {paper && (
        <>
          <p className="scholar-item-meta">
            <span className="scholar-source">{SOURCE_LABELS[paper.source]}</span>
            {paper.category && <span>{paper.category}</span>}
            {paper.citations != null && <span>被引 {paper.citations.toLocaleString()}</span>}
          </p>

          <h1 lang="en" className="scholar-detail-title">
            <MathText text={paper.title} />
          </h1>

          <dl className="scholar-detail-meta">
            <dt>作者</dt>
            <dd>{paper.authors.join(', ') || '—'}</dd>
            <dt>来源</dt>
            <dd>{paper.venue || '—'}</dd>
            <dt>年份</dt>
            <dd>{paper.year ?? '—'}</dd>
            {paper.doi && (
              <>
                <dt>DOI</dt>
                <dd>
                  <a href={`https://doi.org/${paper.doi}`} target="_blank" rel="noreferrer">
                    {paper.doi}
                  </a>
                </dd>
              </>
            )}
            {paper.keywords.length > 0 && (
              <>
                <dt>关键词</dt>
                <dd className="scholar-keywords">
                  {paper.keywords.map((k) => (
                    <span key={k}>{k}</span>
                  ))}
                </dd>
              </>
            )}
          </dl>

          <h2 className="scholar-detail-h2">摘要</h2>
          <p lang="en" className="scholar-detail-abstract">
            <MathText text={paper.abstract || '该文献暂无摘要。'} />
          </p>

          <div className="scholar-actions">
            <button
              className="btn primary"
              onClick={() => onImport(paper)}
              disabled={importing}
              title="下载开放获取 PDF，导入文献库后自动解析版面"
            >
              <IconDownload />
              {importing ? '正在导入…' : '导入文献库并阅读'}
            </button>

            {pdfUrl ? (
              <a className="btn ghost" href={pdfUrl} target="_blank" rel="noreferrer">
                打开 PDF
              </a>
            ) : paper.doi ? (
              <button className="btn ghost" onClick={lookupFullText} disabled={looking}>
                <IconSpark />
                {looking ? '查询中…' : '查找免费全文'}
              </button>
            ) : (
              <span className="scholar-disabled-note">暂无免费全文</span>
            )}

            {oa?.landing_url && (
              <a className="btn ghost" href={oa.landing_url} target="_blank" rel="noreferrer">
                开放获取页面 ↗
              </a>
            )}

            <details className="scholar-cite" ref={citeRef}>
              <summary className="btn ghost">{copied ? `已复制 ${copied} ✓` : '复制引用'}</summary>
              <div className="scholar-cite-menu">
                {CITATION_STYLES.map((style) => (
                  <button key={style.k} type="button" onClick={() => copy(style.k, style.l)}>
                    {style.l}
                  </button>
                ))}
              </div>
            </details>

            <a className="btn ghost" href={paper.url} target="_blank" rel="noreferrer">
              原文页 ↗
            </a>
          </div>

          {notice && <p className="scholar-notice">{notice}</p>}
        </>
      )}
    </div>
  )
}
