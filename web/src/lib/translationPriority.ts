import type { Block } from '../types'

/** Visible blocks first, followed by a small amount of reading-ahead. */
export function priorityBlocks(blocks: Block[], visibleIds: string[], pdfMode: boolean, page: number): string[] {
  const eligible = blocks.filter((b) => (b.translatable || b.kind === 'caption') && b.text.trim())
  if (!pdfMode && visibleIds.length) {
    const visible = new Set(visibleIds)
    const last = eligible.reduce((found, b, i) => visible.has(b.id) ? i : found, -1)
    if (last >= 0) {
      return [...eligible.filter((b) => visible.has(b.id)), ...eligible.slice(last + 1, last + 4)]
        .filter((b) => !b.translated).map((b) => b.id)
    }
  }
  return eligible.filter((b) => !b.translated && (b.page === page || b.page === page + 1 || b.bboxes?.some((rect) => rect[0] === page)))
    .slice(0, 12).map((b) => b.id)
}
