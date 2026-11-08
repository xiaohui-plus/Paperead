import type { ReactElement } from 'react'
import { IconBook, IconList, IconSearch, IconSettings, IconSpark } from './icons'

type NavKey = 'library' | 'search' | 'research' | 'review'

interface Props {
  active: 'library' | 'search'
  /** 文献库条数；检索页不传就不显示角标 */
  count?: number
  onNav: (key: 'library' | 'search') => void
  onSettings: () => void
}

const NAV: { key: NavKey; label: string; enabled: boolean; icon: () => ReactElement }[] = [
  { key: 'library', label: '文献库', enabled: true, icon: IconBook },
  { key: 'search', label: '文献检索', enabled: true, icon: IconSearch },
  { key: 'research', label: '研究', enabled: false, icon: IconSpark },
  { key: 'review', label: '写综述报告', enabled: false, icon: IconList },
]

export function Sidebar({ active, count, onNav, onSettings }: Props) {
  return (
    <aside className="library-side">
      <div className="library-brand">
        <span className="brand-mark">R</span>
        <span className="brand-name">
          paper<i>·</i>reflow
        </span>
      </div>

      <nav className="library-nav">
        {NAV.map((item) => {
          const Icon = item.icon
          return (
            <button
              key={item.key}
              className="library-nav-item"
              data-active={item.key === active}
              disabled={!item.enabled}
              title={item.enabled ? '' : '即将上线'}
              onClick={() => {
                if (item.key === 'library' || item.key === 'search') onNav(item.key)
              }}
            >
              <Icon />
              <span>{item.label}</span>
              {item.key === 'library' && count != null && <em>{count}</em>}
            </button>
          )
        })}
      </nav>

      <div className="library-folders">
        <div className="folders-head">
          <span>文件夹</span>
          <button className="icon-btn" title="新建文件夹（即将上线）" disabled>
            <IconSpark />
          </button>
        </div>
        <p className="folders-empty">还没有文件夹</p>
      </div>

      <button className="library-settings" onClick={onSettings}>
        <IconSettings />
        模型与密钥
      </button>
    </aside>
  )
}
