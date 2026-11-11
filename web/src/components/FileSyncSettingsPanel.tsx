import type { ReactNode } from 'react'
import type { FileSyncTarget } from './FileSyncDialog'
import zoteroIcon from '../assets/zotero.png'
import scholareadIcon from '../assets/scholaread.png'
import phoneIcon from '../assets/phone.svg'

const TARGETS: Array<{
  id: FileSyncTarget
  name: string
  badge: string
  description: string
  action: string
  icon: ReactNode
}> = [
  {
    id: 'zotero',
    name: 'Zotero',
    badge: '双向',
    description:
      '查看 Zotero 里带 PDF 的论文，选择后拉取到文献库；也可以把本地新增的论文、分类与 PDF 推送到 Zotero 云端。',
    action: '打开 Zotero 同步',
    icon: <img className="file-sync-target-logo" src={zoteroIcon} alt="" />,
  },
  {
    id: 'scholaread',
    name: '学术阅读（Scholaread）',
    badge: '拉取 + 写回',
    description:
      '选择学术阅读里的论文导入文献库，并同步阅读进度；退出学术阅读后，可把 Paperead 的进度写回。',
    action: '打开学术阅读同步',
    icon: <img className="file-sync-target-logo" src={scholareadIcon} alt="" />,
  },
  {
    id: 'phone',
    name: '手机',
    badge: '推送到手机',
    description:
      '把文献导出成不依赖网络的 .paperead 同步包，发到手机后导入离线库。建议一次 5–10 篇。',
    action: '打开手机同步',
    icon: <img className="file-sync-target-logo" src={phoneIcon} alt="" />,
  },
]

export function FileSyncSettingsPanel({
  onOpen,
}: {
  onOpen: (target: FileSyncTarget) => void
}) {
  return (
    <div className="modal-body file-sync-settings">
      <section className="model-section">
        <div className="section-head">
          <h3>文件同步</h3>
          <span>Zotero、学术阅读与手机统一入口</span>
        </div>
        <p className="model-empty">
          点开任意目标，会打开一个同步窗口：先查看目标里有哪些论文，再勾选拉取，
          或在支持的方向上推送本地论文。同步只在本机与你选择的软件之间进行。
        </p>
        <div className="file-sync-target-grid">
          {TARGETS.map((item) => (
            <article className="file-sync-target" key={item.id}>
              <div className="file-sync-target-head">
                <span className="file-sync-target-icon">{item.icon}</span>
                <div>
                  <b>{item.name}</b>
                  <span className="file-sync-target-badge">{item.badge}</span>
                </div>
              </div>
              <p>{item.description}</p>
              <button
                type="button"
                className="btn"
                onClick={() => onOpen(item.id)}
              >
                {item.action}
              </button>
            </article>
          ))}
        </div>
      </section>

      <section className="model-section">
        <div className="section-head">
          <h3>同步范围与限制</h3>
        </div>
        <ul className="file-sync-limits">
          <li>Zotero：按条目选择拉取；推送会执行双向同步，把本地新增论文和分类推到 Zotero 云端。</li>
          <li>学术阅读：按论文选择拉取；写回阅读进度是整批执行，学术阅读运行中只能拉取，写回前会自动备份。</li>
          <li>手机：电脑 → 手机单向同步；手机上的笔记和阅读进度不会回传电脑。</li>
          <li>同名论文在目标端会覆盖旧副本；同步不会删除另一端的论文。</li>
        </ul>
      </section>
    </div>
  )
}