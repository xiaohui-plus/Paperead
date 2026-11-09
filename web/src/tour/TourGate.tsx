import { useEffect, useRef } from 'react'

interface Props {
  onStart: () => void
  onSkip: () => void
}

/** 桌面端首次启动的唯一一次选择卡；网页端不会加载这个组件。 */
export function TourGate({ onStart, onSkip }: Props) {
  const primary = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    primary.current?.focus()
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onSkip()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onSkip])

  return (
    <div className="tour-gate" role="dialog" aria-modal aria-labelledby="tour-gate-title">
      <div className="tour-gate-card">
        <div className="tour-gate-brand">
          <span className="tour-gate-mark">P</span>
          <span>Paperead</span>
        </div>
        <h1 id="tour-gate-title">第一次使用 Paperead</h1>
        <p>
          要不要用几分钟走一遍？从上传一篇论文开始，生成导读，再把它变成一份可导出的研究产出。
        </p>
        <div className="tour-gate-actions">
          <button ref={primary} className="tour-btn primary" onClick={onStart}>
            开始教程
          </button>
          <button className="tour-btn ghost" onClick={onSkip}>
            跳过，先自己看看
          </button>
        </div>
        <p className="tour-gate-note">
          只会在首次启动时出现一次。之后可在「设置 → 新手教程」重新打开。
        </p>
      </div>
    </div>
  )
}
