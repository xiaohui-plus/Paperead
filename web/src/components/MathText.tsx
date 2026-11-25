import type { ReactNode } from 'react'
import { renderMathHtml, splitMath } from '../lib/math'
import { stripUnrenderable } from '../lib/text'

interface Props {
  text: string
  /** 纯文本段的渲染回调，用于叠加关键词高亮；缺省直接输出文本 */
  renderText?: (text: string) => ReactNode
  className?: string
}

/**
 * 渲染含 LaTeX 的文本：纯文本段可经 renderText 叠加高亮，公式段用 KaTeX 排版。
 * display 公式在段落内以 block 呈现。
 */
export function MathText({ text, renderText, className }: Props) {
  const parts = splitMath(stripUnrenderable(text))
  if (parts.length === 0) return null

  return (
    <span className={className}>
      {parts.map((part, i) => {
        if (part.type === 'text') {
          return <span key={i}>{renderText ? renderText(part.value) : part.value}</span>
        }
        const html = renderMathHtml(part.value, part.display)
        if (!html) return <span key={i}>{part.value}</span>
        return (
          <span
            key={i}
            className={part.display ? 'math-display' : undefined}
            dangerouslySetInnerHTML={{ __html: html }}
          />
        )
      })}
    </span>
  )
}
