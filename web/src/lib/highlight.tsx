import type { ReactNode } from 'react'

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/**
 * 把译文里命中的术语高亮出来（蓝色虚线下划线 + hover 显示英文原词）。
 * 术语来自后端产出的 glossary，无需额外请求。
 */
export function TermText({
  text,
  terms,
}: {
  text: string
  terms: Record<string, string>
}): ReactNode {
  const entries = Object.entries(terms ?? {})
    .filter(([zh]) => zh.length >= 2)
    .sort((a, b) => b[0].length - a[0].length)

  if (!entries.length || !text) return text

  const lookup = new Map(entries)
  const pattern = new RegExp(`(${entries.map(([zh]) => escapeRe(zh)).join('|')})`, 'g')
  const parts = text.split(pattern)

  return (
    <>
      {parts.map((part, i) =>
        lookup.has(part) ? (
          <mark className="term-hl" key={i} title={lookup.get(part)}>
            {part}
          </mark>
        ) : (
          <span key={i}>{part}</span>
        ),
      )}
    </>
  )
}
