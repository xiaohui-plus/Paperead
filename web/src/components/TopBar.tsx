import type { Paper, ViewMode } from '../types'
import { downloadMarkdown } from '../lib/markdown'
import { IconBack, IconDownload, IconMinus, IconPdf, IconPlus, IconPrint, IconSettings } from './icons'

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
}: Props) {
  const pct = total ? Math.round((translated / total) * 100) : 0

  return (
    <header className="topbar">
      {onBack && (
        <button className="icon-btn back-btn" onClick={onBack} title="返回文献库">
          <IconBack />
        </button>
      )}
      <div className="brand">
        <span className="brand-mark">R</span>
        <span className="brand-name">
          paper<i>·</i>reflow
        </span>
      </div>

      <div className="divider" />

      <div className="doc-title">
        {paper.title}
        {isDemo && <span className="pill" style={{ marginLeft: 10 }}>示例数据</span>}
      </div>

      <div className="tools">
        {!pdfMode && (
          <div className="segmented" role="tablist" aria-label="阅读模式">
            {MODES.map((m) => (
              <button
                key={m.key}
                role="tab"
                aria-selected={mode === m.key}
                data-active={mode === m.key}
                disabled={!hasTranslation && m.key !== 'en'}
                title={!hasTranslation && m.key !== 'en' ? '这篇还没有译文' : undefined}
                onClick={() => onMode(m.key)}
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

        <button
          className="icon-btn"
          title="导出 Markdown"
          onClick={() => downloadMarkdown(paper)}
        >
          <IconDownload />
        </button>
        <button className="icon-btn" title="打印 / 存为 PDF" onClick={() => window.print()}>
          <IconPrint />
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
    </header>
  )
}
