import { useEffect } from 'react'
import { IconClose } from './icons'
import { PackageExportPanel } from './DeviceSync'

interface Props {
  open: boolean
  onClose: () => void
}

/** 文献库 → 发送到手机：默认只打包还没发送过的新论文。 */
export function SendToPhoneDialog({ open, onClose }: Props) {
  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null

  return (
    <div className="send-phone-backdrop" onClick={onClose}>
      <div
        className="send-phone-dialog"
        role="dialog"
        aria-modal="true"
        aria-label="发送到手机"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="send-phone-head">
          <div>
            <h2>发送到手机</h2>
            <p>导出同步包，传到手机后直接离线阅读，不需要同一 Wi-Fi。</p>
          </div>
          <button className="icon-btn" title="关闭" onClick={onClose}>
            <IconClose />
          </button>
        </header>
        <div className="send-phone-body">
          <PackageExportPanel pendingFirst />
          <ol className="send-phone-steps">
            <li>
              <b>1 导出</b>
              <span>保存到桌面或你选择的位置</span>
            </li>
            <li>
              <b>2 传到手机</b>
              <span>微信文件传输助手 / 数据线 / 网盘</span>
            </li>
            <li>
              <b>3 手机导入</b>
              <span>文献库 → 导入同步包</span>
            </li>
          </ol>
        </div>
      </div>
    </div>
  )
}