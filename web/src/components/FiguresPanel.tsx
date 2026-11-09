import type { Block } from '../types'
import { prepareBlocks } from '../lib/blocks'

interface Props {
  blocks: Block[]
  onJump: (id: string) => void
}

/** 图注编号：Figure 3 / TABLE II / Table S1。 */
const LABEL_RE =
  /^(?:Supplementary\s+)?(?:Figure|Fig\.|FIGURE|Table|TABLE|Tab\.)\s*(?:\d+(?:\.\d+)*|[IVXLCDM]+|[A-Z]\d+)/i

/** 译文里可能保留「表 II：」「图 1：」前缀，展示时去掉。 */
const CN_PREFIX_RE =
  /^(?:Supplementary\s+)?(?:Figure|Fig\.?|Table|Tab\.?|图|表)\s*(?:\d+(?:\.\d+)*|[IVXLCDM]+|[A-Z]\d+)\s*[:：.]?\s*/i

function labelOf(b: Block): string {
  const raw = (b.caption || b.text || '').trim()
  return raw.match(LABEL_RE)?.[0] ?? (b.kind === 'table' ? 'Table' : 'Figure')
}

function stripLabel(text: string): string {
  return (text || '').replace(CN_PREFIX_RE, '').trim()
}

export function FiguresPanel({ blocks, onJump }: Props) {
  // 复用阅读流的配对结果：figure/table 块本身只有英文 caption，
  // 中文图注在配对的 caption 块里（captionZh）。
  const items = prepareBlocks(blocks).filter(
    ({ block: b }) =>
      (b.kind === 'figure' || b.kind === 'table') && b.image_path,
  )

  if (!items.length) return <div className="empty">没有识别到图表</div>

  return (
    <div className="gallery">
      {items.map(({ block: b, captionZh, captionEn }) => {
        const original = stripLabel(captionEn || b.caption || '')
        const zh = stripLabel(captionZh || '')
        const name = zh || original || (b.kind === 'table' ? '表格' : '插图')
        return (
          <button
            key={b.id}
            className="gal-item"
            onClick={() => onJump(b.id)}
            title={original || name}
          >
            <div className="gal-thumb">
              <img src={b.image_path ?? ''} alt={name} loading="lazy" />
            </div>
            <div className="gal-cap">
              <b>{labelOf(b)}</b>
              {name}
            </div>
          </button>
        )
      })}
    </div>
  )
}
