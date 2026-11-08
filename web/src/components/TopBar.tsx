import type { Paper, ViewMode } from '../types'
import { downloadMarkdown } from '../lib/markdown'
import { IconBack, IconDownload, IconMinus, IconPlus, IconPrint, IconSettings } from './icons'

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

        <button className="icon-btn" title="缩小字号" onClick={() => onScale(scale - 0.08)}>
          <IconMinus />
        </button>
        <button className="icon-btn" title="放大字号" onClick={() => onScale(scale + 0.08)}>
          <IconPlus />
        </button>

        {!configured && (
          <button className="pill warn" onClick={onOpenSettings} title="未配置模型，翻译不会启动">
            未配置模型
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
          title={configured ? '模型与密钥（已配置）' : '模型与密钥（未配置）'}
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
