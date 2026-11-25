import { useSyncExternalStore, useEffect, useState, type ReactElement, type ReactNode } from 'react'
import {
  BrandMark,
  IconBook,
  IconChevron,
  IconCompass,
  IconEdit,
  IconFolderPlus,
  IconList,
  IconSearch,
  IconSettings,
  IconUpdate,
} from './icons'
import { isMobileShell } from '../lib/mobile'
import { installUpdateInApp } from '../lib/update'
import {
  getUpdateCenterState,
  markUpdateInstalling,
  reportUpdateInstallError,
  subscribeUpdateCenter,
} from '../lib/updateCenter'

export type NavKey = 'library' | 'notes' | 'search' | 'research' | 'review'

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
  { key: 'notes', label: '笔记广场', icon: IconEdit },
  { key: 'search', label: '文献检索', icon: IconSearch },
  { key: 'research', label: '研究', icon: IconCompass },
  { key: 'review', label: '写综述报告', icon: IconList },
]

const COLLAPSE_KEY = 'paperead.sidebarCollapsed'

function readCollapsed(): boolean {
  try {
    return localStorage.getItem(COLLAPSE_KEY) === '1'
  } catch {
    return false
  }
}

export function Sidebar({
  active,
  count,
  onNav,
  onSettings,
  projectSection,
  folderSection,
}: Props) {
  const mobile = isMobileShell()
  const [collapsed, setCollapsed] = useState(() => !mobile && readCollapsed())
  const update = useSyncExternalStore(
    subscribeUpdateCenter,
    getUpdateCenterState,
    getUpdateCenterState,
  )
  const updateReady = update.stage === 'ready' && Boolean(update.version)
  const updateBusy = update.stage === 'installing' || update.stage === 'launching'
  const updateVisible = updateReady || updateBusy

  const installPrepared = () => {
    if (!updateReady || updateBusy) return
    markUpdateInstalling()
    void installUpdateInApp().catch((error) => {
      reportUpdateInstallError(
        typeof error === 'string' ? error : String((error as any)?.message || ''),
      )
    })
  }

  useEffect(() => {
    if (mobile) {
      document.documentElement.removeAttribute('data-sidebar')
      return
    }
    document.documentElement.dataset.sidebar = collapsed ? 'collapsed' : 'expanded'
  }, [collapsed, mobile])

  const toggle = () => {
    const next = !collapsed
    setCollapsed(next)
    try {
      localStorage.setItem(COLLAPSE_KEY, next ? '1' : '0')
    } catch {
      // 隐私模式下写不了本地存储，折叠仍对当前会话生效
    }
  }

  return (
    <aside className="library-side" data-collapsed={collapsed}>
      <div className="library-brand">
        <BrandMark />
        <span className="brand-name">Paperead</span>
        {!mobile && (
          <button
            className="library-collapse"
            data-collapsed={collapsed}
            title={collapsed ? '展开侧栏' : '折叠侧栏'}
            aria-label={collapsed ? '展开侧栏' : '折叠侧栏'}
            aria-expanded={!collapsed}
            onClick={toggle}
          >
            <IconChevron />
          </button>
        )}
      </div>

      <div className="library-side-body">
        <nav className="library-nav">
        {NAV.filter(
          // 安卓端不提供研究与综述
          (item) => !mobile || (item.key !== 'research' && item.key !== 'review'),
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
                <IconFolderPlus />
              </button>
            </div>
            <p className="folders-empty">还没有文件夹</p>
          </div>
        )}
      </div>

      {updateVisible && (
        <button
          className="library-update"
          title={updateBusy ? '正在安装更新…' : `安装新版本 v${update.version}`}
          aria-label={updateBusy ? '正在安装更新' : `安装新版本 v${update.version}`}
          disabled={updateBusy}
          onClick={installPrepared}
        >
          <IconUpdate />
        </button>
      )}

      <button className="library-settings" data-tour="settings-entry" onClick={onSettings}>
        <IconSettings />
        <span>模型与密钥</span>
      </button>
    </aside>
  )
}