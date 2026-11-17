import { useCallback, useEffect, useState } from 'react'
import {
  createPairingCode,
  listPlugins,
  revokePlugin,
  startPairingWait,
  type PairingCode,
  type PluginInfo,
} from '../lib/plugins'

function formatTime(value: number) {
  if (!value) return '—'
  return new Date(value * 1000).toLocaleString()
}

export function PluginSettingsPanel() {
  const [plugins, setPlugins] = useState<PluginInfo[]>([])
  const [pairing, setPairing] = useState<PairingCode | null>(null)
  const [waitingExpires, setWaitingExpires] = useState(0)
  const [knownIds, setKnownIds] = useState<string[]>([])
  const [secondsLeft, setSecondsLeft] = useState(0)
  const [loading, setLoading] = useState(false)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')

  const refresh = useCallback(async () => {
    try {
      const rows = await listPlugins()
      setPlugins(rows)
      setError('')
      return rows
    } catch (err) {
      setError(err instanceof Error ? err.message : '读取插件列表失败')
      return []
    }
  }, [])

  useEffect(() => {
    void refresh().then((rows) => {
      if (rows.length) return
      // 还没连接过插件：自动开一个等待窗口，扩展会在后台认领。
      void startPairingWait()
        .then(({ expires_at }) => {
          setKnownIds([])
          setWaitingExpires(expires_at)
        })
        .catch(() => undefined)
    })
  }, [refresh])

  // 等待扩展认领：轮询插件列表，出现新插件即视为连接成功
  useEffect(() => {
    if (!waitingExpires) return
    let stopped = false

    const tick = async () => {
      const left = Math.max(0, Math.ceil(waitingExpires - Date.now() / 1000))
      setSecondsLeft(left)
      if (left === 0) {
        setWaitingExpires(0)
        setError('等待超时，请重试')
        return
      }
      const rows = await refresh()
      if (stopped) return
      if (rows.some((row) => !knownIds.includes(row.id))) {
        setWaitingExpires(0)
        setNotice('插件已连接')
      }
    }

    void tick()
    const timer = window.setInterval(() => void tick(), 1500)
    return () => {
      stopped = true
      window.clearInterval(timer)
    }
  }, [waitingExpires, knownIds, refresh])

  // 手动配对码倒计时
  useEffect(() => {
    if (!pairing) return
    const timer = window.setInterval(() => {
      const left = Math.max(0, Math.ceil(pairing.expires_at - Date.now() / 1000))
      setSecondsLeft(left)
      if (left === 0) {
        window.clearInterval(timer)
        setPairing(null)
      }
    }, 1000)
    setSecondsLeft(Math.max(0, Math.ceil(pairing.expires_at - Date.now() / 1000)))
    return () => window.clearInterval(timer)
  }, [pairing])

  const startWait = async () => {
    setLoading(true)
    setNotice('')
    setPairing(null)
    try {
      const { expires_at } = await startPairingWait()
      setKnownIds(plugins.map((plugin) => plugin.id))
      setWaitingExpires(expires_at)
      setError('')
    } catch (err) {
      setError(err instanceof Error ? err.message : '开启等待失败')
    } finally {
      setLoading(false)
    }
  }

  const generate = async () => {
    setLoading(true)
    setNotice('')
    setWaitingExpires(0)
    try {
      const code = await createPairingCode()
      setPairing(code)
      setError('')
    } catch (err) {
      setError(err instanceof Error ? err.message : '生成配对码失败')
    } finally {
      setLoading(false)
    }
  }

  const remove = async (plugin: PluginInfo) => {
    if (!window.confirm(`撤销「${plugin.name}」的访问权限？`)) return
    try {
      await revokePlugin(plugin.id)
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : '撤销失败')
    }
  }

  return (
    <div className="modal-body plugin-settings">
      <section className="model-section">
        <div className="section-head">
          <h3>浏览器插件</h3>
          <span>把网页上的论文送进本地文献库</span>
        </div>
        <p className="model-empty">
          Paperead Connector 只读取当前网页的论文信息和 PDF，并通过本机接口存入文献库。
          插件不能访问模型配置、研究项目或文件系统。
          安装好扩展后，后台会自动连接，不需要输入配对码或手动点连接。
        </p>
        <div className="plugin-install-links">
          <span>安装：</span>
          <a href="https://github.com/xiaohui-plus/Paperead" target="_blank" rel="noreferrer">
            Chrome / Edge 构建说明 ↗
          </a>
        </div>
      </section>

      <section className="model-section">
        <div className="section-head">
          <h3>连接浏览器插件</h3>
          <span>后台自动连接</span>
        </div>
        {waitingExpires ? (
          <div className="plugin-pair-box">
            <b className="plugin-waiting">等待扩展连接…</b>
            <span>
              扩展会在后台自动连接，无需打开弹窗；剩余 {secondsLeft} 秒
            </span>
            <button className="link-btn" onClick={() => setWaitingExpires(0)}>
              取消
            </button>
          </div>
        ) : (
          <button className="btn primary" onClick={() => void startWait()} disabled={loading}>
            {loading ? '准备中…' : '连接浏览器插件'}
          </button>
        )}
        {notice && <p className="plugin-notice">{notice}</p>}
        {error && <p className="plugin-error">{error}</p>}

        <details className="plugin-manual">
          <summary>手动输入配对码</summary>
          {pairing ? (
            <div className="plugin-pair-box">
              <b className="plugin-pair-code">{pairing.code}</b>
              <span>在插件弹窗中输入这 6 位数字，剩余 {secondsLeft} 秒</span>
            </div>
          ) : (
            <button className="link-btn" onClick={() => void generate()} disabled={loading}>
              {loading ? '生成中…' : '生成 6 位配对码'}
            </button>
          )}
        </details>
      </section>

      <section className="model-section">
        <div className="section-head">
          <h3>已连接的插件</h3>
          <span>{plugins.length} 个</span>
        </div>
        {plugins.length === 0 ? (
          <p className="model-empty">
            还没有插件连接。安装扩展并保持 Paperead 打开，扩展会在后台自动连接。
          </p>
        ) : (
          <div className="plugin-list">
            {plugins.map((plugin) => (
              <article className="plugin-card" key={plugin.id}>
                <div>
                  <b>{plugin.name}</b>
                  <span>{plugin.origin}</span>
                  <span>最近使用：{formatTime(plugin.last_used_at)}</span>
                </div>
                <button className="link-btn danger" onClick={() => void remove(plugin)}>
                  撤销
                </button>
              </article>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}
