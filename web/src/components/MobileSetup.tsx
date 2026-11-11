import { useState } from 'react'
import {
  loadMobileServer,
  saveMobileServer,
  setOfflineMode,
  testMobileServer,
} from '../lib/mobile'
import { MobileDeviceSyncPanel } from './DeviceSync'

interface FormProps {
  base: string
  token: string
  onBase: (value: string) => void
  onToken: (value: string) => void
}

function ServerForm({ base, token, onBase, onToken }: FormProps) {
  return (
    <>
      <label className="mobile-setup-field">
        <span>服务器地址</span>
        <input
          value={base}
          onChange={(event) => onBase(event.target.value)}
          placeholder="http://192.168.1.10:8010"
          inputMode="url"
          autoCapitalize="off"
          autoCorrect="off"
          spellCheck={false}
        />
      </label>
      <label className="mobile-setup-field">
        <span>访问口令（可选）</span>
        <input
          value={token}
          onChange={(event) => onToken(event.target.value)}
          placeholder="服务端设置了 PAPEREAD_TOKEN 时填写"
          type="password"
          autoCapitalize="off"
          autoCorrect="off"
          spellCheck={false}
        />
      </label>
    </>
  )
}

/** 安卓端首次启动：同步包导入优先，Wi-Fi 直连作为可选方式。 */
export function MobileSetupScreen() {
  return (
    <div className="mobile-setup">
      <div className="mobile-setup-card">
        <div className="mobile-setup-brand">
          <span className="brand-mark">P</span>
          <span className="brand-name">Paperead</span>
        </div>

        <div className="mobile-setup-intro">
          <h1>把电脑上的文献放到手机</h1>
          <p>
            电脑导出一个同步包，手机选文件导入即可。不需要同一 Wi-Fi，也不用配置服务器。
          </p>
        </div>

        <ol className="mobile-setup-steps">
          <li>
            <b>电脑导出同步包</b>
            <span>打开 Paperead → 设置 → 设备同步 → 文件同步</span>
          </li>
          <li>
            <b>把 .paperead 发到手机</b>
            <span>微信文件传输助手、数据线或网盘都可以</span>
          </li>
          <li>
            <b>在下方选择文件导入</b>
            <span>导入后回到文献库即可离线阅读</span>
          </li>
        </ol>

        <MobileDeviceSyncPanel
          bare
          onDone={() => {
            setOfflineMode()
            window.location.reload()
          }}
        />

        <button
          className="btn ghost mobile-setup-skip"
          type="button"
          onClick={() => {
            setOfflineMode()
            window.location.reload()
          }}
        >
          先跳过，直接使用离线文献库
        </button>

        <details className="mobile-setup-manual">
          <summary>想用 Wi-Fi 直连？手动填写服务器地址</summary>
          <ManualConnect />
        </details>
      </div>
    </div>
  )
}

/** 手动连接：公网域名或扫码不方便时使用。 */
function ManualConnect() {
  const saved = loadMobileServer()
  const [base, setBase] = useState(saved.base)
  const [token, setToken] = useState(saved.token)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      await testMobileServer(base, token)
      saveMobileServer(base, token)
      window.location.reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setBusy(false)
    }
  }

  return (
    <form className="mobile-setup-form" onSubmit={submit}>
      <ServerForm base={base} token={token} onBase={setBase} onToken={setToken} />
      {error && <p className="mobile-setup-error">{error}</p>}
      <button className="btn primary mobile-setup-submit" type="submit" disabled={busy}>
        {busy ? '正在连接…' : '连接'}
      </button>
    </form>
  )
}

/** 设置页里的「服务器连接」：随时切换后端地址。 */
export function MobileServerPanel() {
  const saved = loadMobileServer()
  const [base, setBase] = useState(saved.base)
  const [token, setToken] = useState(saved.token)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      await testMobileServer(base, token)
      saveMobileServer(base, token)
      window.location.reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setBusy(false)
    }
  }

  return (
    <form className="modal-body mobile-server-panel" onSubmit={submit}>
      <p className="settings-hint">
        修改后会重新加载页面。地址只保存在本机，不会上传。
      </p>
      <ServerForm base={base} token={token} onBase={setBase} onToken={setToken} />
      {error && <p className="mobile-setup-error">{error}</p>}
      <div className="settings-actions">
        <button className="btn primary" type="submit" disabled={busy}>
          {busy ? '正在连接…' : '保存并重新加载'}
        </button>
      </div>
    </form>
  )
}
