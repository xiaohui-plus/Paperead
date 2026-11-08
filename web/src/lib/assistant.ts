import type { Block, Paper } from '../types'

export interface KeyPoint {
  id: string
  title: string
  text: string
}

/** 章节速览：每个一/二级标题，取其后第一段正文的首句。 */
export function keyPoints(blocks: Block[]): KeyPoint[] {
  const out: KeyPoint[] = []
  for (let i = 0; i < blocks.length; i++) {
    const b = blocks[i]
    if (b.kind !== 'heading' || b.level > 2) continue

    const next = blocks.slice(i + 1).find((x) => x.kind === 'para' && x.text.length > 40)
    if (!next) continue

    const zh = (next.translated || next.text).replace(/\s+/g, ' ').trim()
    out.push({
      id: b.id,
      title: b.translated || b.text,
      text: firstSentence(zh),
    })
  }
  return out
}

function firstSentence(text: string): string {
  const m = text.match(/^.{16,140}?[。！？.!?](\s|$)/)
  const s = (m?.[0] ?? text.slice(0, 140)).trim()
  return s.length < text.length ? s : s
}

export type AskKind = 'summary' | 'outline' | 'guide' | 'faq'

function abstractText(paper: Paper): string {
  const idx = paper.blocks.findIndex(
    (b) => b.kind === 'heading' && /abstract|摘要/i.test(b.text),
  )
  if (idx < 0) return ''
  const para = paper.blocks
    .slice(idx + 1)
    .find((b) => b.kind === 'para' && b.text.length > 80)
  return (para?.translated || para?.text || '').trim()
}

function outlineText(paper: Paper): string {
  const heads = paper.blocks.filter((b) => b.kind === 'heading' && b.level <= 2)
  return heads
    .map((h, i) => `${i + 1}. ${h.translated || h.text}`)
    .join('\n')
}

/**
 * 本地演示用的问答：不依赖模型，直接用解析结果拼答案。
 * 接入后端后把 buildAnswer 换成 SSE 接口即可。
 */
export function buildAnswer(kind: AskKind, paper: Paper): string {
  switch (kind) {
    case 'summary':
      return (
        abstractText(paper) ||
        '这篇论文没有识别到摘要段落，可以试试「梳理论文大纲」。'
      )
    case 'outline':
      return outlineText(paper) || '没有识别到章节标题。'
    case 'guide': {
      const points = keyPoints(paper.blocks).slice(0, 6)
      if (!points.length) return '没有识别到可用的章节结构。'
      return [
        '建议按这个顺序读：',
        ...points.map((p, i) => `${i + 1}. ${p.title} —— ${p.text}`),
      ].join('\n')
    }
    case 'faq':
      return [
        '可以试试这样问：',
        '· 这篇论文的核心创新点是什么？',
        '· 实验用了哪些数据集，指标如何？',
        '· 方法部分的第 2 个公式在解释什么？',
        '',
        `当前为演示模式，收录 ${paper.blocks.filter((b) => b.kind === 'para').length} 个正文段落、` +
          `${paper.blocks.filter((b) => b.kind === 'figure' || b.kind === 'table').length} 个图表。`,
      ].join('\n')
  }
}
