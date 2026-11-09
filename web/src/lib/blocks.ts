import type { Block } from '../types'

export interface RenderBlock {
  block: Block
  /** figure/table 的图注（中文优先） */
  captionZh?: string
  captionEn?: string
}

/** 图注编号：Figure 3 / TABLE II / Table S1。 */
const CAPTION_KEY_RE =
  /^(?:Supplementary\s+)?(?:Figure|Fig\.|FIGURE|Table|TABLE|Tab\.)\s*(?:\d+(?:\.\d+)*|[IVXLCDM]+|[A-Z]\d+)/i

function captionKey(text?: string): string {
  return (text ?? '').trim().match(CAPTION_KEY_RE)?.[0].replace(/\s+/g, '').toLowerCase() ?? ''
}

/**
 * 把「图片块 + 图注块」合成一个渲染单元。
 *
 * 解析器会把 figure/table 和它的 caption 拆成两个 block（图自带 caption 原文，
 * caption block 带译文），而且表题可能排在表格上方（MDPI/IEEE）也可能在下方。
 * 这里前后都找，合并成一次渲染，避免图注重复两遍；同一张裁图也不会渲染两次
 * （翻译流更新、旧数据里重复的图表块都会在这里被去重）。
 */
export function prepareBlocks(blocks: Block[]): RenderBlock[] {
  const usedCaption = new Set<string>()
  const paired = new Map<string, Block>()
  const seenImage = new Set<string>()
  const skip = new Set<string>()

  for (let i = 0; i < blocks.length; i++) {
    const b = blocks[i]
    if (b.kind !== 'figure' && b.kind !== 'table') continue
    // 同一张裁图只渲染一次：旧数据或重复解析里可能留下多个指向同一 PNG 的块
    if (b.image_path && seenImage.has(b.image_path)) {
      skip.add(b.id)
      continue
    }
    if (b.image_path) seenImage.add(b.image_path)

    const key = captionKey(b.caption)
    const match = (c?: Block) =>
      Boolean(
        c &&
          c.kind === 'caption' &&
          !usedCaption.has(c.id) &&
          c.page === b.page &&
          (!key || captionKey(c.text) === key),
      )
    const next = blocks[i + 1]
    const prev = blocks[i - 1]
    const cap = match(next) ? next : match(prev) ? prev : undefined
    if (cap) {
      usedCaption.add(cap.id)
      paired.set(b.id, cap)
    }
  }

  const out: RenderBlock[] = []
  for (const b of blocks) {
    if (skip.has(b.id)) continue
    if (b.kind === 'caption' && usedCaption.has(b.id)) continue
    if (b.kind === 'figure' || b.kind === 'table') {
      const cap = paired.get(b.id)
      out.push({
        block: b,
        captionEn: b.caption || (cap ? cap.text : ''),
        captionZh: cap ? cap.translated : '',
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
