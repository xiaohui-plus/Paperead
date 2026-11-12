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
  onJump: (id: string, page?: number) => void
}

/**
 * 手机端阅读页底部工具条：常驻在屏幕底部，
 * 点其中一项从下方展开对应面板，点遮罩或关闭按钮收起。
 */
export function MobileDock(props: Props) {
  const [open, setOpen] = useState<TabKey | null>(null)
  const active = PANEL_TABS.find((t) => t.key === open)

  // 双击正文里的标注：手机端没有左侧栏，直接展开「批注」面板
  useEffect(() => {
    if (props.focusAnnotation) setOpen('comments')
  }, [props.focusAnnotation])

  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open])

  const close = () => setOpen(null)

  const jump = (id: string, page?: number) => {
    props.onJump(id, page)
    close()
  }

  return (
    <>
      <nav className="mobile-dock" aria-label="阅读工具">
        {PANEL_TABS.map((t) => (
          <button
            key={t.key}
            className="mobile-dock-item"
            data-active={open === t.key}
            aria-pressed={open === t.key}
            onClick={() => setOpen((value) => (value === t.key ? null : t.key))}
          >
            {t.icon}
            <span>{t.label}</span>
          </button>
        ))}
      </nav>

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
