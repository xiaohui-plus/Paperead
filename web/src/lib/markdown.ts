import type { Block, Paper } from '../types'

/** 极简清洗：去掉 script / 事件属性 / javascript 协议。表格来自本机解析器，风险低但不应裸用。 */
export function sanitizeTableHtml(html: string): string {
  return html
    .replace(/<script[\s\S]*?<\/script>/gi, '')
    .replace(/\son\w+\s*=\s*"[^"]*"/gi, '')
    .replace(/\son\w+\s*=\s*'[^']*'/gi, '')
    .replace(/javascript:/gi, '')
}

function blockToMarkdown(b: Block): string {
  switch (b.kind) {
    case 'heading':
      return `\n${'#'.repeat(Math.min(3, Math.max(1, b.level)))} ${b.translated || b.text}\n`
    case 'para':
      return `${b.translated || b.text}\n\n> ${b.text}\n`
    case 'equation':
      return b.image_path ? `\n![equation](${b.image_path})\n` : `\n\`\`\`\n${b.text}\n\`\`\`\n`
    case 'figure':
      return b.image_path ? `\n![figure](${b.image_path})\n` : ''
    case 'table':
      return b.table_html ? `\n${b.table_html}\n` : ''
    case 'keywords':
      return `\n**关键词：** ${b.text.replace(/^keywords?[:：]\s*/i, '')}\n`
    default:
      return ''
  }
}

export function toMarkdown(paper: Paper): string {
  const lines: string[] = [`# ${paper.title}`, '', `*${paper.authors}*`, '']
  for (const b of paper.blocks) {
    if (['title', 'author', 'affiliation', 'reference', 'footnote'].includes(b.kind)) continue
    const md = blockToMarkdown(b)
    if (md) lines.push(md)
  }
  return lines.join('\n')
}

export function downloadMarkdown(paper: Paper, filename = 'paper.md'): void {
  const blob = new Blob([toMarkdown(paper)], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}
