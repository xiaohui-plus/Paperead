import type { ReactElement, ReactNode } from 'react'
import {
  IconBook,
  IconList,
  IconSearch,
  IconSettings,
  IconSpark,
} from './icons'
import { isMobileShell } from '../lib/mobile'

export type NavKey = 'library' | 'search' | 'research' | 'review'

interface Props {
  active: NavKey
  /** 文献库条数；检索页不传就不显示角标 */
  count?: number
  onNav: (key: NavKey) => void
  onSettings: () => void
  /** 研究/写综述时用它替换“文件夹”分区 */
  projectSection?: ReactNode
  /** 文献库用它渲染文件夹分区 */
  folderSection?: ReactNode
}

const NAV: { key: NavKey; label: string; icon: () => ReactElement }[] = [
  { key: 'library', label: '文献库', icon: IconBook },
  { key: 'search', label: '文献检索', icon: IconSearch },
  { key: 'research', label: '研究', icon: IconSpark },
  { key: 'review', label: '写综述报告', icon: IconList },
]

export function Sidebar({
  active,
  count,
  onNav,
  onSettings,
  projectSection,
  folderSection,
}: Props) {
  return (
    <aside className="library-side">
      <div className="library-brand">
        <span className="brand-mark">P</span>
        <span className="brand-name">Paperead</span>
      </div>

      <nav className="library-nav">
        {NAV.filter(
          // 安卓端不提供研究与综述
          (item) => !isMobileShell() || (item.key !== 'research' && item.key !== 'review'),
        ).map((item) => {
          const Icon = item.icon
          return (
            <button
              key={item.key}
              className="library-nav-item"
              data-active={item.key === active}
              data-tour={`nav-${item.key}`}
              title={item.label}
              aria-label={item.label}
              onClick={() => onNav(item.key)}
            >
              <Icon />
              <span>{item.label}</span>
              {item.key === 'library' && count != null && <em>{count}</em>}
            </button>
          )
        })}
      </nav>

      {projectSection ?? folderSection ?? (
        <div className="library-folders">
          <div className="folders-head">
            <span>文件夹</span>
            <button className="icon-btn" title="新建文件夹（即将上线）" disabled>
              <IconSpark />
            </button>
          </div>
          <p className="folders-empty">还没有文件夹</p>
        </div>
      )}

      <button className="library-settings" data-tour="settings-entry" onClick={onSettings}>
        <IconSettings />
        模型与密钥
      </button>
    </aside>
  )
}
