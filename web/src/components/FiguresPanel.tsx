import type { Block } from '../types'

interface Props {
  blocks: Block[]
  onJump: (id: string) => void
}

function labelOf(b: Block): string {
  const raw = (b.caption || b.text || '').trim()
  const m = raw.match(/^(Figure|Table|Fig\.|Tab\.)\s*\d+/i)
  return m ? m[0] : b.kind === 'table' ? 'Table' : 'Figure'
}

export function FiguresPanel({ blocks, onJump }: Props) {
  const items = blocks.filter(
    (b) => (b.kind === 'figure' || b.kind === 'table') && b.image_path,
  )

  if (!items.length) return <div className="empty">没有识别到图表</div>

  return (
    <div className="gallery">
      {items.map((b) => {
        const caption = (b.caption || '').replace(/^(Figure|Table|Fig\.|Tab\.)\s*\d+\s*[:：]\s*/i, '')
        return (
          <button key={b.id} className="gal-item" onClick={() => onJump(b.id)}>
            <div className="gal-thumb">
              <img src={b.image_path ?? ''} alt={caption} loading="lazy" />
            </div>
            <div className="gal-cap">
              <b>{labelOf(b)}</b>
              {caption || (b.kind === 'table' ? '表格' : '插图')}
            </div>
          </button>
        )
      })}
    </div>
  )
}
