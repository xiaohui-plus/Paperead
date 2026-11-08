import type { Block } from '../types'

export interface RenderBlock {
  block: Block
  /** figure/table 的图注（中文优先） */
  captionZh?: string
  captionEn?: string
}

/**
 * 把「图片块 + 紧随其后的图注块」合成一个渲染单元。
 *
 * 解析器会把 figure 和它的 caption 拆成前后两个 block（figure 自带 caption 原文，
 * caption block 带译文）。直接顺序渲染会出现"图注重复两遍"，这里合并掉。
 */
export function prepareBlocks(blocks: Block[]): RenderBlock[] {
  const out: RenderBlock[] = []
  const consumed = new Set<string>()

  for (let i = 0; i < blocks.length; i++) {
    const b = blocks[i]
    if (consumed.has(b.id)) continue

    if (b.kind === 'figure' || b.kind === 'table') {
      const next = blocks[i + 1]
      const paired =
        next && next.kind === 'caption' && next.page === b.page && !consumed.has(next.id)
      if (paired) consumed.add(next.id)
      out.push({
        block: b,
        captionEn: b.caption || (paired ? next.text : ''),
        captionZh: paired ? next.translated : '',
      })
      continue
    }

    out.push({ block: b })
  }

  return out
}

/** 目录：一级 + 二级标题 */
export function outlineOf(blocks: Block[]) {
  return blocks.filter((b) => b.kind === 'heading' && b.level <= 2)
}

export function isFlowBlock(b: Block): boolean {
  return !['title', 'author', 'affiliation', 'reference', 'footnote'].includes(b.kind)
}
