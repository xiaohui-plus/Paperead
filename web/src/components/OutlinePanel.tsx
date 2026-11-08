import type { Block } from '../types'

interface Props {
  items: Block[]
  activeId: string
  onJump: (id: string) => void
}

export function OutlinePanel({ items, activeId, onJump }: Props) {
  if (!items.length) return <div className="empty">没有识别到章节结构</div>

  return (
    <div>
      {items.map((b, i) => (
        <button
          key={b.id}
          className="outline-item"
          data-active={activeId === b.id}
          data-level={b.level}
          onClick={() => onJump(b.id)}
          title={`第 ${b.page} 页`}
        >
          <span className="idx">{String(i + 1).padStart(2, '0')}</span>
          <span>{b.translated || b.text}</span>
        </button>
      ))}
    </div>
  )
}
