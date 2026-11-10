import { useState, type ReactNode } from 'react'
import type { Block, Paper, ReaderContextItem } from '../types'
import type { KeyPoint } from '../lib/assistant'
import type { ModelKind } from '../lib/settings'
import type { Annotation, AnnotationColor, ReadingHighlight } from '../lib/library'
import { tourBus } from '../tour/tourBus'
import { ChatPanel } from './ChatPanel'
import { PaperAttachments } from './PaperAttachments'
import { CommentsPanel } from './CommentsPanel'
import { OutlinePanel } from './OutlinePanel'
import { KeyPointsPanel } from './KeyPointsPanel'
import { FiguresPanel } from './FiguresPanel'
import { GuidePanel } from './GuidePanel'
import { LineagePanel } from './LineagePanel'
import {
  IconChat,
  IconComment,
  IconFolder,
  IconImage,
  IconKey,
  IconList,
  IconSpark,
} from './icons'

export type TabKey =
  | 'guide'
  | 'chat'
  | 'files'
  | 'outline'
  | 'key'
  | 'comments'
  | 'lineage'
  | 'figures'

export interface PanelProps {
  paperId: string
  paper: Paper
  outline: Block[]
  points: KeyPoint[]
  activeId: string
  onJump: (id: string) => void
  onOpenSettings: (kind?: ModelKind) => void
  settingsVersion?: number
  annotations: Annotation[]
  onUpdateAnnotation: (
    id: string,
    patch: { color?: AnnotationColor; note?: string },
  ) => Promise<void>
  onDeleteAnnotation: (id: string) => Promise<void>
  /** 自动提取的研究目标 / 方法 / 结论 */
  highlights: ReadingHighlight[]
  /** 阅读页点小圆圈加入的图表/公式，随问题一起提交 */
  contextItems?: ReaderContextItem[]
  onRemoveContext?: (id: string) => void
  onClearContext?: () => void
}

export const PANEL_TABS: { key: TabKey; label: string; icon: ReactNode }[] = [
  { key: 'guide', label: '导读', icon: <IconSpark /> },
  { key: 'chat', label: '问答', icon: <IconChat /> },
  { key: 'files', label: '附件', icon: <IconFolder /> },
  { key: 'outline', label: '目录', icon: <IconList /> },
  { key: 'lineage', label: '脉络', icon: <IconKey /> },
  { key: 'key', label: '阅读重点', icon: <IconKey /> },
  { key: 'comments', label: '批注', icon: <IconComment /> },
  { key: 'figures', label: '图表', icon: <IconImage /> },
]

function initialTab(): TabKey {
  const q = new URLSearchParams(window.location.search).get('tab')
  return (PANEL_TABS.some((t) => t.key === q) ? q : 'guide') as TabKey
}

/** 左侧栏与手机端底部面板共用的内容区。 */
export function PanelBody({
  tab,
  paperId,
  paper,
  outline,
  points,
  activeId,
  onJump,
  onOpenSettings,
  settingsVersion = 0,
  annotations,
  onUpdateAnnotation,
  onDeleteAnnotation,
  highlights,
  contextItems = [],
  onRemoveContext,
  onClearContext,
}: PanelProps & { tab: TabKey }) {
  if (tab === 'chat') {
    return (
      <ChatPanel
        paper={paper}
        paperId={paperId}
        onJump={onJump}
        onOpenSettings={() => onOpenSettings('answer')}
        settingsVersion={settingsVersion}
        contextItems={contextItems}
        onRemoveContext={onRemoveContext}
        onClearContext={onClearContext}
      />
    )
  }

  if (tab === 'files') {
    return <PaperAttachments paperId={paperId} />
  }

  if (tab === 'comments') {
    return (
      <CommentsPanel
        paper={paper}
        annotations={annotations}
        onJump={onJump}
        onUpdate={onUpdateAnnotation}
        onDelete={onDeleteAnnotation}
      />
    )
  }

  return (
    <div className="tab-body">
      {tab === 'guide' && (
        <GuidePanel
          paperId={paperId}
          onOpenSettings={() => onOpenSettings('answer')}
          settingsVersion={settingsVersion}
        />
      )}
      {tab === 'lineage' && (
        <LineagePanel
          paperId={paperId}
          onOpenSettings={() => onOpenSettings('answer')}
          settingsVersion={settingsVersion}
        />
      )}
      {tab === 'outline' && (
        <OutlinePanel items={outline} activeId={activeId} onJump={onJump} />
      )}
      {tab === 'key' && (
        <KeyPointsPanel
          highlights={highlights}
          points={points}
          annotations={annotations}
          onJump={onJump}
        />
      )}
      {tab === 'figures' && <FiguresPanel blocks={paper.blocks} onJump={onJump} />}
    </div>
  )
}

export function LeftPanel(props: PanelProps & { tab?: TabKey; onTab?: (tab: TabKey) => void }) {
  const [innerTab, setInnerTab] = useState(initialTab)
  const tab = props.tab ?? innerTab

  const selectTab = (next: TabKey) => {
    setInnerTab(next)
    props.onTab?.(next)
    tourBus.emit('reader:tab-visited', { tab: next })
  }

  return (
    <aside className="panel">
      <div className="panel-head">
        <span className="spark">
          <IconSpark />
        </span>
        <h2>阅读助手</h2>
      </div>

      <nav className="tabs" data-tour="reader-tabs" role="tablist">
        {PANEL_TABS.map((t) => (
          <button
            key={t.key}
            className="tab"
            role="tab"
            data-tour={`reader-tab-${t.key}`}
            aria-selected={tab === t.key}
            data-active={tab === t.key}
            onClick={() => selectTab(t.key)}
          >
            {t.icon}
            {t.label}
          </button>
        ))}
      </nav>

      <PanelBody {...props} tab={tab} />
    </aside>
  )
}
