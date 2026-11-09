import { useState } from 'react'
import {
  loadMobileServer,
  saveMobileServer,
  testMobileServer,
} from '../lib/mobile'

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

/** 安卓端首次启动：先配置要连接的 Paperead 服务器。 */
export function MobileSetupScreen() {
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
    <div className="mobile-setup">
      <form className="mobile-setup-card" onSubmit={submit}>
        <div className="mobile-setup-brand">
          <span className="brand-mark">P</span>
          <span className="brand-name">Paperead</span>
        </div>
        <h1>连接你的文献库</h1>
        <p>
          手机端负责阅读与检索，文献解析、翻译和问答由你的电脑或服务器完成。
          请填写运行 Paperead 后端的地址。
        </p>
        <ServerForm base={base} token={token} onBase={setBase} onToken={setToken} />
        {error && <p className="mobile-setup-error">{error}</p>}
        <button className="btn primary mobile-setup-submit" type="submit" disabled={busy}>
          {busy ? '正在连接…' : '连接'}
        </button>
        <p className="mobile-setup-hint">
          局域网内填电脑的内网 IP；公网使用请填带 https 的域名。
        </p>
      </form>
    </div>
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
