import { IconBook, IconSettings, IconSpark } from './icons'

interface Props {
  active: 'library'
  count: number
  onSettings: () => void
}

const NAV = [
  { key: 'library', label: '文献库', enabled: true },
  { key: 'research', label: '研究', enabled: false },
  { key: 'review', label: '写综述报告', enabled: false },
  { key: 'cite', label: '参考文献引用助手', enabled: false },
] as const

export function Sidebar({ count, onSettings }: Props) {
  return (
    <aside className="library-side">
      <div className="library-brand">
        <span className="brand-mark">R</span>
        <span className="brand-name">
          paper<i>·</i>reflow
        </span>
      </div>

      <nav className="library-nav">
        {NAV.map((item) => (
          <button
            key={item.key}
            className="library-nav-item"
            data-active={item.key === 'library'}
            disabled={!item.enabled}
            title={item.enabled ? '' : '即将上线'}
          >
            <IconBook />
            <span>{item.label}</span>
            {item.key === 'library' && <em>{count}</em>}
          </button>
        ))}
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
