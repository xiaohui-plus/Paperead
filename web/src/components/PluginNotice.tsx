import { AlertCircle, CheckCircle2, Info, Loader2 } from 'lucide-react'
import type { ReactNode } from 'react'

export type PluginNoticeTone = 'info' | 'ok' | 'warn' | 'error' | 'busy'

interface Props {
  tone?: PluginNoticeTone
  title?: ReactNode
  children?: ReactNode
  action?: ReactNode
}

const ICONS = {
  info: Info,
  ok: CheckCircle2,
  warn: AlertCircle,
  error: AlertCircle,
  busy: Loader2,
} as const

/**
 * 插件安装/连接的统一提醒。
 *
 * 只用一块浅底 + 细边框承载结构，颜色落在图标与标题上，不再用整块黄底/红底；
 * 标题、说明、操作三段固定顺序，避免各页面各写一套导致层级和间距不一致。
 */
export function PluginNotice({ tone = 'info', title, children, action }: Props) {
  const Icon = ICONS[tone]
  return (
    <div className="plugin-note" data-tone={tone} role={tone === 'error' ? 'alert' : 'status'}>
      <Icon className={`plugin-note-icon${tone === 'busy' ? ' spin' : ''}`} size={15} aria-hidden />
      <div className="plugin-note-text">
        {title ? <b>{title}</b> : null}
        {children ? <p>{children}</p> : null}
        {action ? <div className="plugin-note-action">{action}</div> : null}
      </div>
    </div>
  )
}