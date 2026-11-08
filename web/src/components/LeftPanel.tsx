import { useState, type ReactNode } from 'react'
import type { Block, Paper } from '../types'
import type { KeyPoint } from '../lib/assistant'
import { ChatPanel } from './ChatPanel'
import { OutlinePanel } from './OutlinePanel'
import { KeyPointsPanel } from './KeyPointsPanel'
import { FiguresPanel } from './FiguresPanel'
import { GuidePanel } from './GuidePanel'
import { LineagePanel } from './LineagePanel'
import { IconChat, IconImage, IconKey, IconList, IconSpark } from './icons'

type TabKey = 'guide' | 'chat' | 'outline' | 'key' | 'lineage' | 'figures'

interface Props {
  paperId: string
  paper: Paper
  outline: Block[]
  points: KeyPoint[]
  activeId: string
  onJump: (id: string) => void
  onOpenSettings: () => void
}

const TABS: { key: TabKey; label: string; icon: ReactNode }[] = [
  { key: 'guide', label: '导读', icon: <IconSpark /> },
  { key: 'chat', label: '问答', icon: <IconChat /> },
  { key: 'outline', label: '目录', icon: <IconList /> },
  { key: 'lineage', label: '脉络', icon: <IconKey /> },
  { key: 'key', label: '重点', icon: <IconKey /> },
  { key: 'figures', label: '图表', icon: <IconImage /> },
]

function initialTab(): TabKey {
  const q = new URLSearchParams(window.location.search).get('tab')
  return (TABS.some((t) => t.key === q) ? q : 'guide') as TabKey
}

export function LeftPanel({
  paperId,
  paper,
  outline,
  points,
  activeId,
  onJump,
  onOpenSettings,
}: Props) {
  const [tab, setTab] = useState<TabKey>(initialTab)

  return (
    <aside className="panel">
      <div className="panel-head">
        <span className="spark">
          <IconSpark />
        </span>
        <h2>阅读助手</h2>
      </div>

      <nav className="tabs" role="tablist">
        {TABS.map((t) => (
          <button
            key={t.key}
            className="tab"
            role="tab"
            aria-selected={tab === t.key}
            data-active={tab === t.key}
            onClick={() => setTab(t.key)}
          >
            {t.icon}
            {t.label}
          </button>
        ))}
      </nav>

      {tab === 'chat' ? (
        <ChatPanel
          paper={paper}
          paperId={paperId}
          onJump={onJump}
          onOpenSettings={onOpenSettings}
        />
      ) : (
        <div className="tab-body">
          {tab === 'guide' && (
            <GuidePanel paperId={paperId} onOpenSettings={onOpenSettings} />
          )}
          {tab === 'lineage' && (
            <LineagePanel paperId={paperId} onOpenSettings={onOpenSettings} />
          )}
          {tab === 'outline' && (
            <OutlinePanel items={outline} activeId={activeId} onJump={onJump} />
          )}
          {tab === 'key' && <KeyPointsPanel points={points} onJump={onJump} />}
          {tab === 'figures' && <FiguresPanel blocks={paper.blocks} onJump={onJump} />}
        </div>
      )}
    </aside>
  )
}
