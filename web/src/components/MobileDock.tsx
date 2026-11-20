import { useEffect, useRef, useState } from 'react'
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

const NARROW_QUERY = '(max-width: 860px)'

function readNarrow(): boolean {
  return typeof window !== 'undefined' && window.matchMedia(NARROW_QUERY).matches
}

/**
 * 手机端阅读页底部工具条：常驻在屏幕底部，
 * 点其中一项从下方展开对应面板，点遮罩或关闭按钮收起。
 *
 * 只在窄屏渲染：桌面端有左侧栏，双击标注/阅读重点时不能再从正文下方
 * 叠一个抽屉出来（那会和左侧栏重复）。
 */
export function MobileDock(props: Props) {
  const [open, setOpen] = useState<TabKey | null>(null)
  const [narrow, setNarrow] = useState(readNarrow)
  const lastFocus = useRef(props.focusAnnotation)
  const active = PANEL_TABS.find((t) => t.key === open)

  useEffect(() => {
    const mq = window.matchMedia(NARROW_QUERY)
    const onChange = () => setNarrow(mq.matches)
    onChange()
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])

  // 拉宽到桌面宽度后抽屉已不可见，下次缩窄时不应再自动弹出来
  useEffect(() => {
    if (!narrow) setOpen(null)
  }, [narrow])

  // 双击正文里的标注：只有手机端没有左侧栏时才展开底部「批注」面板；
  // 桌面端只切左侧栏，不能把面板再叠到正文下方。
  // 只在 focus 真的更新时展开：窗口刚缩窄时不能拿上次双击的旧目标自动弹抽屉。
  useEffect(() => {
    if (!narrow) {
      lastFocus.current = props.focusAnnotation
      return
    }
    if (!props.focusAnnotation || props.focusAnnotation === lastFocus.current) return
    lastFocus.current = props.focusAnnotation
    setOpen('comments')
    // 抽屉有入场动画，等它落定后再滚一次，确保目标卡片真的进入可视区
    const id = props.focusAnnotation.id
    const timer = window.setTimeout(() => {
      document
        .getElementById(`note-${id}`)
        ?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }, 260)
    return () => window.clearTimeout(timer)
  }, [props.focusAnnotation, narrow])

  // 双击自动阅读重点：同上，桌面端只切左侧「阅读重点」
  useEffect(() => {
    if (!narrow) return
    if (props.focusHighlight) setOpen('key')
  }, [props.focusHighlight, narrow])

  // 阅读页划词点「提问」：窄屏没有左侧栏，直接从下方展开「问答」
  useEffect(() => {
    if (!narrow) return
    if (props.focusChat) setOpen('chat')
  }, [props.focusChat, narrow])

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

  // 桌面端左侧栏就是唯一入口，底部工具条与抽屉一律不渲染
  if (!narrow) return null

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
