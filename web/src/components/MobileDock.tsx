import { useEffect, useState } from 'react'
import {
  PANEL_TABS,
  PanelBody,
  type PanelProps,
  type TabKey,
} from './LeftPanel'
import { IconClose } from './icons'

interface Props extends PanelProps {
  /** 打开面板时先把阅读区滚到对应段落，再收起面板。 */
  onJump: (id: string) => void
}

/**
 * 手机端阅读页底部工具条：
 * 平时收起，鼠标移到屏幕底部或点一下把手才升起；
 * 点其中一项从下方展开对应面板，点遮罩或关闭按钮收起。
 */
export function MobileDock(props: Props) {
  const [revealed, setRevealed] = useState(false)
  const [open, setOpen] = useState<TabKey | null>(null)
  const show = revealed || open !== null
  const active = PANEL_TABS.find((t) => t.key === open)

  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open])

  const close = () => {
    setOpen(null)
    setRevealed(false)
  }

  const jump = (id: string) => {
    props.onJump(id)
    close()
  }

  return (
    <>
      <div
        className="mobile-dock-wrap"
        data-show={show}
        data-open={open !== null}
        onMouseEnter={() => setRevealed(true)}
        onMouseLeave={() => {
          if (!open) setRevealed(false)
        }}
      >
        <button
          className="mobile-dock-handle"
          aria-label={show ? '收起阅读工具' : '展开阅读工具'}
          aria-expanded={show}
          onClick={() => setRevealed((value) => !value)}
        >
          <span />
        </button>
        <nav className="mobile-dock" aria-label="阅读工具">
          {PANEL_TABS.map((t) => (
            <button
              key={t.key}
              className="mobile-dock-item"
              data-active={open === t.key}
              aria-pressed={open === t.key}
              onClick={() => {
                setRevealed(true)
                setOpen((value) => (value === t.key ? null : t.key))
              }}
            >
              {t.icon}
              <span>{t.label}</span>
            </button>
          ))}
        </nav>
      </div>

      {open !== null && (
        <>
          <button
            className="mobile-sheet-scrim"
            aria-label="关闭面板"
            onClick={close}
          />
          <section
            className="mobile-sheet"
            role="dialog"
            aria-modal="true"
            aria-label={active?.label ?? '阅读工具'}
          >
            <header className="mobile-sheet-head">
              <span className="mobile-sheet-grip" aria-hidden />
              <h2>{active?.label}</h2>
              <button
                className="mobile-sheet-close"
                aria-label="关闭"
                onClick={close}
              >
                <IconClose />
              </button>
            </header>
            <div className="mobile-sheet-body">
              <PanelBody {...props} onJump={jump} tab={open} />
            </div>
          </section>
        </>
      )}
    </>
  )
}
