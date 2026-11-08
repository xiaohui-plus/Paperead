import { useEffect, useRef, useState, type ReactNode } from 'react'
import { MathText } from './MathText'
import {
  SOURCE_LABELS,
  highlightTerms,
  truncateText,
  type ScholarPaper,
} from '../lib/scholar'

interface Props {
  paper: ScholarPaper
  /** 当前检索词，用于高亮 */
  query?: string
  onOpen: (paper: ScholarPaper) => void
}

function highlight(text: string, terms: string[]): ReactNode {
  if (!terms.length) return text
  const split = new RegExp(`(${terms.join('|')})`, 'gi')
  return text.split(split).map((part, i) =>
    i % 2 ? (
      <mark key={i} className="scholar-mark">
        {part}
      </mark>
    ) : (
      part
    ),
  )
}

/**
 * 摘要预览：默认两行，鼠标停留 0.8s 在下方原地展开全文，移开即收起。
 * 展开区域在同一个 hover 容器内，鼠标移入不会触发收起，避免来回闪动。
 */
function AbstractPreview({
  text,
  renderText,
}: {
  text: string
  renderText?: (text: string) => ReactNode
}) {
  const [open, setOpen] = useState(false)
  const timer = useRef<number | null>(null)

  const cancel = () => {
    if (timer.current !== null) {
      window.clearTimeout(timer.current)
      timer.current = null
    }
  }
  useEffect(() => cancel, [])

  return (
    <div
      className="scholar-abstract"
      onMouseEnter={() => {
        cancel()
        timer.current = window.setTimeout(() => setOpen(true), 800)
      }}
      onMouseLeave={() => {
        cancel()
        setOpen(false)
      }}
    >
      <p lang="en" className="scholar-abstract-line">
        <MathText text={truncateText(text, 360)} renderText={renderText} />
      </p>
      {open && (
        <p lang="en" className="scholar-abstract-full">
          <MathText text={text} renderText={renderText} />
        </p>
      )}
    </div>
  )
}

export function ScholarResult({ paper, query = '', onOpen }: Props) {
  const terms = highlightTerms(query)
  const renderText = terms.length ? (text: string) => highlight(text, terms) : undefined
  const authors = paper.authors.slice(0, 4).join(', ')

  return (
    <article className="scholar-item">
      <p className="scholar-item-meta">
        <span className="scholar-source">{SOURCE_LABELS[paper.source]}</span>
        {paper.category && <span>{paper.category}</span>}
        {paper.citations != null && <span>被引 {paper.citations.toLocaleString()}</span>}
      </p>

      <button
        type="button"
        className="scholar-item-title"
        lang="en"
        onClick={() => onOpen(paper)}
        title="查看详情"
      >
        <MathText text={paper.title} renderText={renderText} />
      </button>

      <p className="scholar-item-authors">
        {authors || '作者未知'}
        {paper.authors.length > 4 ? ' 等' : ''} · {paper.venue}
        {paper.year ? ` · ${paper.year}` : ''}
      </p>

      {paper.abstract && <AbstractPreview text={paper.abstract} renderText={renderText} />}
    </article>
  )
}
