import { useEffect, useRef, useState } from 'react'
import type { Paper, ViewMode } from '../types'
import { downloadMarkdown } from '../lib/markdown'
import { tourBus } from '../tour/tourBus'
import { IconBack, IconCollapse, IconDownload, IconExpand, IconMinus, IconPdf, IconPlus, IconPrint, IconSettings, IconSidebar } from './icons'

interface Props {
  paper: Paper
  isDemo: boolean
  mode: ViewMode
  onMode: (m: ViewMode) => void
  scale: number
  onScale: (next: number) => void
  translated: number
  total: number
  onOpenSettings: () => void
  configured: boolean
  onBack?: () => void
  hasTranslation?: boolean
  translating?: boolean
  page?: number
  /** 有可查看的原始 PDF（示例数据没有） */
  canShowPdf: boolean
  pdfMode: boolean
  onTogglePdf: () => void
  pdfHref: string
  pdfExpanded: boolean
  onTogglePdfExpand: () => void
  /** 阅读侧栏折叠状态与切换；不传则不显示折叠按钮。 */
  panelCollapsed?: boolean
  onTogglePanel?: () => void
  showPanelToggle?: boolean
}

const MODES: { key: ViewMode; label: string }[] = [
  { key: 'bilingual', label: '对照' },
  { key: 'zh', label: '译文' },
  { key: 'en', label: '原文' },
]

export function TopBar({
  paper,
  isDemo,
  mode,
  onMode,
  scale,
  onScale,
  translated,
  total,
  onOpenSettings,
  configured,
  onBack,
  hasTranslation = true,
  translating = false,
  page,
  canShowPdf,
  pdfMode,
  onTogglePdf,
  pdfHref,
  pdfExpanded,
  onTogglePdfExpand,
  panelCollapsed = false,
  onTogglePanel,
  showPanelToggle = false,
}: Props) {
  const pct = total ? Math.round((translated / total) * 100) : 0

  const [busy, setBusy] = useState<'print' | 'download' | null>(null)
  const [progress, setProgress] = useState('')
  const [toast, setToast] = useState<{ text: string; kind: 'ok' | 'err' } | null>(null)
  const toastTimer = useRef<number | null>(null)
  const downloadRef = useRef<HTMLDetailsElement>(null)

  const notify = (text: string, kind: 'ok' | 'err' = 'ok') => {
    if (toastTimer.current) window.clearTimeout(toastTimer.current)
    setToast({ text, kind })
    toastTimer.current = window.setTimeout(() => setToast(null), 3200)
  }

  const closeDownloadMenu = () => {
    if (downloadRef.current) downloadRef.current.open = false
  }

  // 点下拉菜单以外的区域时收起
  useEffect(() => {
    const onPointerDown = (event: PointerEvent) => {
      const el = downloadRef.current
      if (el?.open && !el.contains(event.target as Node)) el.open = false
    }
    document.addEventListener('pointerdown', onPointerDown)
    return () => document.removeEventListener('pointerdown', onPointerDown)
  }, [])

  // 示例数据没有 PDF，退回原来的 Markdown 导出
  const handleDownload = async () => {
    closeDownloadMenu()
    if (!canShowPdf) {
      downloadMarkdown(paper)
      return
    }
    if (busy) return
    setBusy('download')
    try {
      const { downloadPdf } = await import('../lib/pdfFile')
      await downloadPdf(pdfHref, paper.title)
      notify('原版 PDF 已保存')
    } catch (err) {
      notify(err instanceof Error ? err.message : 'PDF 下载失败', 'err')
    } finally {
      setBusy(null)
    }
  }

  // 打印整份原版 PDF；没有 PDF 时仍按重排内容打印
  const handlePrint = async () => {
    if (!canShowPdf) {
      window.print()
      return
    }
    if (busy) return
    setBusy('print')
    setProgress('正在准备打印…')
    try {
      const { printPdf } = await import('../lib/pdfFile')
      await printPdf(pdfHref, (done, all) =>
        setProgress(done < all ? `正在准备打印 ${done}/${all}` : '正在打开打印对话框…'),
      )
      notify('已打开打印对话框')
    } catch (err) {
      notify(err instanceof Error ? err.message : '打印准备失败', 'err')
    } finally {
      setBusy(null)
      setProgress('')
    }
  }

  return (
    <header className="topbar">
      {onBack && (
        <button className="icon-btn back-btn" onClick={onBack} title="返回文献库">
          <IconBack />
        </button>
      )}
      <div className="brand">
        <span className="brand-mark">P</span>
        <span className="brand-name">Paperead</span>
      </div>

      <div className="divider" />

      <div className="doc-title">
        {paper.title}
        {isDemo && <span className="pill" style={{ marginLeft: 10 }}>示例数据</span>}
      </div>

      <div className="tools">
        {showPanelToggle && onTogglePanel && (
          <button
            className="icon-btn panel-toggle"
            data-active={!panelCollapsed}
            aria-pressed={!panelCollapsed}
            title={panelCollapsed ? '展开阅读侧栏' : '收起阅读侧栏'}
            onClick={onTogglePanel}
          >
            <IconSidebar />
          </button>
        )}
        {!pdfMode && (
          <div className="segmented" data-tour="reader-mode" role="tablist" aria-label="阅读模式">
            {MODES.map((m) => (
              <button
                key={m.key}
                role="tab"
                aria-selected={mode === m.key}
                data-active={mode === m.key}
                disabled={!hasTranslation && m.key !== 'en'}
                title={!hasTranslation && m.key !== 'en' ? '这篇还没有译文' : undefined}
                onClick={() => {
                  onMode(m.key)
                  tourBus.emit('reader:mode-changed', { mode: m.key })
                }}
              >
                {m.label}
              </button>
            ))}
          </div>
        )}

        <button
          className="icon-btn"
          title={pdfMode ? '缩小页面' : '缩小字号'}
          onClick={() => onScale(scale - 0.08)}
        >
          <IconMinus />
        </button>
        <button
          className="icon-btn"
          title={pdfMode ? '放大页面' : '放大字号'}
          onClick={() => onScale(scale + 0.08)}
        >
          <IconPlus />
        </button>

        {canShowPdf && (
          <button
            className="icon-btn pdf-toggle"
            data-active={pdfMode}
            aria-pressed={pdfMode}
            title={pdfMode ? '返回重排阅读' : '查看原版 PDF'}
            onClick={onTogglePdf}
          >
            <IconPdf />
          </button>
        )}

        {pdfMode && (
          <button
            className="icon-btn"
            data-active={pdfExpanded}
            aria-pressed={pdfExpanded}
            title={pdfExpanded ? '收起全宽，保留左侧面板' : '展开原版到全宽'}
            onClick={onTogglePdfExpand}
          >
            {pdfExpanded ? <IconCollapse /> : <IconExpand />}
          </button>
        )}

        {pdfMode && (
          <a
            className="pill"
            href={pdfHref}
            target="_blank"
            rel="noreferrer"
            title="在新标签页打开 PDF（浏览器自带阅读器）"
          >
            新窗口 ↗
          </a>
        )}

        {!configured && (
          <button
            className="pill warn"
            onClick={onOpenSettings}
            title="未配置回答模型，翻译不会启动"
          >
            未配置回答模型
          </button>
        )}

        <span className="pill" title={`已翻译 ${translated} / ${total} 段`}>
          {page ? <>P{page} · </> : null}
          已译 <b>{translated}</b>/{total}
          {translating && <i className="spin-dot" />}
        </span>

        {progress && <span className="pill">{progress}</span>}

        <details className="topbar-download" ref={downloadRef}>
          <summary
            className="icon-btn"
            title={canShowPdf ? '下载原版 PDF / 导出 Markdown' : '导出 Markdown'}
          >
            {busy === 'download' ? <i className="spin-dot" /> : <IconDownload />}
          </summary>
          <div className="topbar-download-menu">
            {canShowPdf && (
              <button type="button" onClick={handleDownload} disabled={busy !== null}>
                下载原版 PDF
              </button>
            )}
            <button
              type="button"
              onClick={() => {
                closeDownloadMenu()
                downloadMarkdown(paper)
              }}
            >
              导出 Markdown
            </button>
          </div>
        </details>
        <button
          className="icon-btn print-btn"
          title={canShowPdf ? '打印原版 PDF' : '打印 / 存为 PDF'}
          disabled={busy !== null}
          onClick={handlePrint}
        >
          {busy === 'print' ? <i className="spin-dot" /> : <IconPrint />}
        </button>

        <button
          className="icon-btn settings-btn"
          title={configured ? '模型设置（回答模型已配置）' : '模型设置（未配置回答模型）'}
          onClick={onOpenSettings}
        >
          <IconSettings />
          <i className="dot" data-on={configured} />
        </button>
      </div>

      <div className="topbar-progress">
        <i style={{ width: `${pct}%` }} />
      </div>

      {toast && (
        <div className={`toast ${toast.kind}`} onClick={() => setToast(null)}>
          {toast.text}
        </div>
      )}
    </header>
  )
}
