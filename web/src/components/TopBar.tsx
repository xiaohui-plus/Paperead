import { useEffect, useRef, useState } from 'react'
import type { Paper, ViewMode } from '../types'
import { downloadMarkdown } from '../lib/markdown'
import { tourBus } from '../tour/tourBus'
import { IconAutoHighlight, IconBack, IconCollapse, IconDownload, IconExpand, IconExternalLink, IconMinus, IconPdf, IconPlus, IconReview, IconSidebar, IconTranslate } from './icons'

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
  /** PDF 审阅模式：在页面右侧显示批注卡片 */
  pdfReview: boolean
  onTogglePdfReview: () => void
  /** 窄屏没有右侧栏：审阅入口改由底部批注抽屉承载，顶栏不再显示 */
  narrow?: boolean
  /** 阅读侧栏折叠状态与切换；不传则不显示折叠按钮。 */
  panelCollapsed?: boolean
  onTogglePanel?: () => void
  showPanelToggle?: boolean
  /** 自动阅读重点 / 自动翻译的快捷开关 */
  autoHighlights: boolean
  onToggleAutoHighlights: () => void
  autoTranslate: boolean
  onToggleAutoTranslate: () => void
  /** 全文翻译双栏模式 */
  dualMode?: boolean
  /** PDF 视图：在「原文 / 译文 / 对照」之间切换 */
  onDualViewMode?: (mode: 'source' | 'zh' | 'dual') => void
  /** 双栏：是否显示左侧原文，供「原文 / 译文 / 对照」选中态使用 */
  dualShowSource?: boolean
  dualSync?: boolean
  onToggleDualSync?: () => void
  /** 双栏页面放大倍率（1 = 适配宽度），仅对比阅读使用 */
  dualZoom?: number
  onDualZoom?: (delta: number) => void
  onDualZoomReset?: () => void
  /** 双栏译文 PDF 下载（只在对比阅读中提供） */
  translatedPdfReady?: boolean
  translatedPdfUrl?: string
  translationLang?: string
}

const MODES: { key: ViewMode; label: string }[] = [
  { key: 'bilingual', label: '对照' },
  { key: 'zh', label: '译文' },
  { key: 'en', label: '原文' },
]

const PDF_MODES: { key: 'source' | 'zh' | 'dual'; label: string; title: string }[] = [
  { key: 'source', label: '原文', title: '只看原版 PDF' },
  { key: 'zh', label: '译文', title: '只看译文 PDF' },
  { key: 'dual', label: '对照', title: '左原文 / 右译文对照' },
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
  pdfReview,
  onTogglePdfReview,
  narrow = false,
  panelCollapsed = false,
  onTogglePanel,
  showPanelToggle = false,
  autoHighlights,
  onToggleAutoHighlights,
  autoTranslate,
  onToggleAutoTranslate,
  dualMode = false,
  onDualViewMode,
  dualShowSource = true,
  dualSync = true,
  onToggleDualSync,
  dualZoom = 1,
  onDualZoom,
  onDualZoomReset,
  translatedPdfReady = false,
  translatedPdfUrl = '',
  translationLang = 'zh-Hans',
}: Props) {
  const pct = total ? Math.round((translated / total) * 100) : 0

  const [busy, setBusy] = useState<'download' | 'translated' | null>(null)
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

  /** 双栏专用：把译文 PDF 另存到用户选择的位置；取消保存不算失败。 */
  const handleDownloadTranslated = async () => {
    if (!translatedPdfUrl || busy) return
    setBusy('translated')
    try {
      const { downloadTranslatedPdf } = await import('../lib/pdfFile')
      const saved = await downloadTranslatedPdf(
        translatedPdfUrl,
        paper.title,
        translationLang,
        paper.paper_id,
      )
      if (saved) notify('译文 PDF 已保存')
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') return
      notify(err instanceof Error ? err.message : '译文 PDF 下载失败', 'err')
    } finally {
      setBusy(null)
    }
  }


  const pdfModeActive = (key: 'source' | 'zh' | 'dual') =>
    key === 'source'
      ? !dualMode
      : key === 'zh'
        ? dualMode && !dualShowSource
        : dualMode && dualShowSource

  return (
    <header className="topbar">
      {onBack && (
        <button className="icon-btn back-btn" onClick={onBack} title="返回文献库">
          <IconBack />
        </button>
      )}


      <div className="doc-title">
        {paper.title}
        {isDemo && <span className="pill" style={{ marginLeft: 10 }}>示例数据</span>}
      </div>

      <div className="tools">
        {showPanelToggle && onTogglePanel && panelCollapsed && (
          <button
            className="icon-btn panel-toggle"
            title="展开阅读侧栏"
            aria-label="展开阅读侧栏"
            onClick={onTogglePanel}
          >
            <IconSidebar />
          </button>
        )}
        <button
          className="icon-btn tool-toggle"
          data-active={autoHighlights}
          aria-pressed={autoHighlights}
          title={autoHighlights ? '自动阅读重点：已开启，点击关闭' : '自动阅读重点：已关闭，点击开启'}
          onClick={onToggleAutoHighlights}
        >
          <IconAutoHighlight />
        </button>
        {!pdfMode && !dualMode && (
          <button
            className="icon-btn tool-toggle"
            data-active={autoTranslate}
            aria-pressed={autoTranslate}
            title={autoTranslate ? '自动翻译：已开启，点击关闭' : '自动翻译：已关闭，点击开启'}
            onClick={onToggleAutoTranslate}
          >
            <IconTranslate />
          </button>
        )}
        {canShowPdf && onDualViewMode && (pdfMode || dualMode) && (
          <div className="segmented" role="tablist" aria-label="PDF 阅读模式">
            {PDF_MODES.map((m) => {
              const active = pdfModeActive(m.key)
              return (
                <button
                  key={m.key}
                  role="tab"
                  aria-selected={active}
                  data-active={active}
                  title={m.title}
                  onClick={() => onDualViewMode(m.key)}
                >
                  {m.label}
                </button>
              )
            })}
          </div>
        )}
        {!pdfMode && !dualMode && (
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
          title={dualMode ? '缩小页面（左右同步）' : pdfMode ? '缩小页面' : '缩小字号'}
          onClick={() => (dualMode ? onDualZoom?.(-0.25) : onScale(scale - 0.08))}
        >
          <IconMinus />
        </button>
        <button
          className="icon-btn"
          title={dualMode ? '放大页面（左右同步）' : pdfMode ? '放大页面' : '放大字号'}
          onClick={() => (dualMode ? onDualZoom?.(0.25) : onScale(scale + 0.08))}
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

        {pdfMode && !narrow && (
          <button
            className="icon-btn pdf-review-toggle"
            data-active={pdfReview}
            aria-pressed={pdfReview}
            title={pdfReview ? '退出审阅模式' : '审阅模式：在页面右侧显示批注'}
            onClick={onTogglePdfReview}
          >
            <IconReview />
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
            className="icon-btn"
            href={pdfHref}
            target="_blank"
            rel="noreferrer"
            aria-label="在新标签页打开 PDF"
            title="在新标签页打开 PDF（浏览器自带阅读器）"
          >
            <IconExternalLink />
          </a>
        )}

        {!configured && (
          <button
            className="pill warn"
            onClick={onOpenSettings}
            title="未配置回答模型，翻译不会启动"
          >
            未配置模型
          </button>
        )}

        <span className="pill" title={`已翻译 ${translated} / ${total} 段`}>
          {page ? <>P{page} · </> : null}
          <b>{translated}</b>/{total}
          {translating && <i className="spin-dot" />}
        </span>


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


        {dualMode && (
          <div className="topbar-dual-actions" role="group" aria-label="双栏阅读控制">
            {onDualZoomReset && (
              <button
                type="button"
                className="dual-source-toggle dual-zoom"
                data-active={dualZoom > 1}
                title={
                  dualZoom > 1
                    ? `当前放大 ${Math.round(dualZoom * 100)}%，点击恢复适配宽度`
                    : '适配宽度（100%）'
                }
                onClick={onDualZoomReset}
              >
                {Math.round(dualZoom * 100)}%
              </button>
            )}

            {onToggleDualSync && (
              <button
                type="button"
                className="dual-source-toggle"
                data-active={dualSync}
                aria-pressed={dualSync}
                title={dualSync ? '左右滚动联动：已开启，点击关闭' : '左右滚动联动：已关闭，点击开启'}
                onClick={onToggleDualSync}
              >
                联动
              </button>
            )}
            {translatedPdfReady && translatedPdfUrl && (
              <button
                type="button"
                className="icon-btn topbar-translated-download"
                title="下载译文 PDF（选择保存位置）"
                disabled={busy !== null}
                onClick={handleDownloadTranslated}
              >
                {busy === 'translated' ? <i className="spin-dot" /> : <IconDownload />}
              </button>
            )}
          </div>
        )}

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
