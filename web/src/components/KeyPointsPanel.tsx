import type { KeyPoint } from '../lib/assistant'

interface Props {
  points: KeyPoint[]
  onJump: (id: string) => void
}

export function KeyPointsPanel({ points, onJump }: Props) {
  if (!points.length) return <div className="empty">没有识别到章节要点</div>

  return (
    <div>
      <p className="quick-label" style={{ margin: '0 0 8px' }}>
        按章节抽取的首句，可快速扫读
      </p>
      {points.map((p) => (
        <button key={p.id} className="kp-item" onClick={() => onJump(p.id)}>
          <h4>{p.title}</h4>
          <p>{p.text}</p>
        </button>
      ))}
    </div>
  )
}
