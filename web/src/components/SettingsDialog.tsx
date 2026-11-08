import { useEffect, useState } from 'react'
import { isConfigured, loadSettings, PRESETS, saveSettings, type LLMSettings } from '../lib/settings'
import { Katex } from './Katex'
import { IconCheck, IconClose } from './icons'

interface Props {
  open: boolean
  onClose: () => void
  sampleImage?: string
  onSaved?: (s: LLMSettings) => void
}

type Status =
  | { kind: 'idle' }
  | { kind: 'testing' }
  | { kind: 'ok'; ms: number; formulaMs?: number; latex?: string }
  | { kind: 'error'; message: string }

async function blobToBase64(blob: Blob): Promise<string> {
  const buf = await blob.arrayBuffer()
  let binary = ''
  const bytes = new Uint8Array(buf)
  for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i])
  return btoa(binary)
}

async function postJson(url: string, body: unknown): Promise<any> {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`)
  return data
}

export function SettingsDialog({ open, onClose, sampleImage, onSaved }: Props) {
  const [form, setForm] = useState<LLMSettings>(loadSettings)
  const [showKey, setShowKey] = useState(false)
  const [status, setStatus] = useState<Status>({ kind: 'idle' })

  useEffect(() => {
    if (open) {
      setForm(loadSettings())
      setStatus({ kind: 'idle' })
    }
  }, [open])

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null

  const set = (patch: Partial<LLMSettings>) => setForm((f) => ({ ...f, ...patch }))

  const runTest = async () => {
    setStatus({ kind: 'testing' })
    try {
      const ping = await postJson('/api/ping', { config: form })
      let latex: string | undefined
      let formulaMs: number | undefined
      if (sampleImage) {
        try {
          const blob = await fetch(sampleImage).then((r) => r.blob())
          const data = await postJson('/api/formula', {
            config: form,
            image_base64: await blobToBase64(blob),
          })
          latex = data.latex || ''
          formulaMs = data.ms
        } catch {
          // 公式试识别失败不否定连通性
        }
      }
      setStatus({ kind: 'ok', ms: ping.ms, formulaMs, latex })
    } catch (e) {
      setStatus({ kind: 'error', message: e instanceof Error ? e.message : String(e) })
    }
  }

  const submit = () => {
    saveSettings(form)
    onSaved?.(form)
    onClose()
  }

  return (
    <div className="modal-mask" onMouseDown={onClose}>
      <div className="modal" onMouseDown={(e) => e.stopPropagation()} role="dialog" aria-modal>
        <header className="modal-head">
          <div>
            <h2>模型与密钥</h2>
            <p>用于公式识别（图片 → LaTeX）。配置保存在本机浏览器。</p>
          </div>
          <button className="icon-btn" onClick={onClose} title="关闭">
            <IconClose />
          </button>
        </header>

        <div className="modal-body">
          <label className="field-label">服务商</label>
          <div className="presets">
            {PRESETS.map((p) => (
              <button
                key={p.id}
                className="preset"
                data-active={form.baseUrl === p.baseUrl && form.model === p.model}
                onClick={() => set({ baseUrl: p.baseUrl, model: p.model })}
                title={p.note}
              >
                <b>{p.label}</b>
                <span>{p.note}</span>
              </button>
            ))}
          </div>

          <label className="field-label" htmlFor="base">接口地址</label>
          <input
            id="base"
            className="input"
            value={form.baseUrl}
            onChange={(e) => set({ baseUrl: e.target.value })}
            placeholder="https://open.bigmodel.cn/api/paas/v4"
            spellCheck={false}
          />

          <label className="field-label" htmlFor="model">模型名</label>
          <input
            id="model"
            className="input"
            value={form.model}
            onChange={(e) => set({ model: e.target.value })}
            placeholder="glm-4.6v"
            spellCheck={false}
          />

          <label className="field-label" htmlFor="key">API Key</label>
          <div className="input-row">
            <input
              id="key"
              className="input"
              type={showKey ? 'text' : 'password'}
              value={form.apiKey}
              onChange={(e) => set({ apiKey: e.target.value })}
              placeholder="粘贴你的 API Key"
              spellCheck={false}
              autoComplete="off"
            />
            <button className="ghost-btn" onClick={() => setShowKey((v) => !v)}>
              {showKey ? '隐藏' : '显示'}
            </button>
          </div>

          <p className="hint">
            密钥只存在这台机器的 localStorage，请求时随 body 发给本地服务（<code>python -m server.main</code>），
            服务端不落盘。浏览器直连厂商接口会被 CORS 拦，所以必须有这层本地代理。
          </p>

          <div className="test-box" data-state={status.kind}>
            {status.kind === 'idle' && <span>填好后可以点「测试连接」验证地址、密钥、模型名是否匹配。</span>}
            {status.kind === 'testing' && <span>正在连接…</span>}
            {status.kind === 'ok' && (
              <div>
                <div className="test-ok">
                  <IconCheck /> 连接正常 · {status.ms}ms
                  {status.formulaMs != null && ` · 公式识别 ${status.formulaMs}ms`}
                </div>
                {status.latex ? (
                  <div className="test-latex">
                    <Katex tex={status.latex} />
                    <code>{status.latex}</code>
                  </div>
                ) : (
                  sampleImage && <div className="test-warn">连通性正常，但示例公式没识别出结果（可能是模型不支持图像输入）。</div>
                )}
              </div>
            )}
            {status.kind === 'error' && (
              <div className="test-err">
                失败：{status.message}
                <br />
                常见原因：本地服务没启动 / 地址末尾多了 <code>/v1</code> / 模型名不对 / Key 无效。
              </div>
            )}
          </div>
        </div>

        <footer className="modal-foot">
          <button className="btn" onClick={onClose}>取消</button>
          <button className="btn primary" onClick={runTest} disabled={!isConfigured(form) || status.kind === 'testing'}>
            测试连接
          </button>
          <button className="btn primary" onClick={submit} disabled={!isConfigured(form)}>
            保存
          </button>
        </footer>
      </div>
    </div>
  )
}
