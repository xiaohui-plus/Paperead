import katex from 'katex'
import type { ReactNode } from 'react'
import { splitMath } from '../lib/math'

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
  const parts = splitMath(text)
  if (parts.length === 0) return null

  return (
    <span className={className}>
      {parts.map((part, i) =>
        part.type === 'text' ? (
          <span key={i}>{renderText ? renderText(part.value) : part.value}</span>
        ) : (
          <span
            key={i}
            className={part.display ? 'math-display' : undefined}
            // KaTeX 输出的是受信任的静态标记（未开启 trust），可安全注入
            dangerouslySetInnerHTML={{
              __html: katex.renderToString(part.value, {
                throwOnError: false,
                displayMode: part.display,
                strict: false,
                trust: false,
              }),
            }}
          />
        ),
      )}
    </span>
  )
}
