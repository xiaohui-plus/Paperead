import { useEffect, useRef, useState } from 'react'
import type { Paper } from '../types'
import { askPaper, type AskSource } from '../lib/library'
import { isConfigured, loadSettings } from '../lib/settings'
import { buildAnswer, type AskKind } from '../lib/assistant'
import { IconSend, IconSpark } from './icons'

interface Msg {
  id: number
  role: 'user' | 'ai'
  text: string
  sources?: AskSource[]
  usedWeb?: boolean
  latency?: number
  error?: boolean
}

const QUICK: { key: AskKind; label: string }[] = [
  { key: 'summary', label: '总结论文' },
  { key: 'outline', label: '梳理论文大纲' },
  { key: 'guide', label: '生成一份学习指南' },
  { key: 'faq', label: '常见问题解答' },
]

export function ChatPanel({
  paper,
  paperId,
  onJump,
  onOpenSettings,
}: {
  paper: Paper
  paperId: string
  onJump?: (id: string) => void
  onOpenSettings?: () => void
}) {
  const [messages, setMessages] = useState<Msg[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [useWeb, setUseWeb] = useState(true)
  const listRef = useRef<HTMLDivElement>(null)
  const seq = useRef(0)

  useEffect(() => {
    const el = listRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages, busy])

  // 深链 ?ask=xxx 自动提问（也方便分享一个问题）
  const asked = useRef(false)
  useEffect(() => {
    if (asked.current) return
    const q = new URLSearchParams(window.location.search).get('ask')
    if (!q) return
    asked.current = true
    void run(q)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const push = (msg: Omit<Msg, 'id'>) => {
    seq.current += 1
    setMessages((prev) => [...prev, { id: seq.current, ...msg }])
  }

  const run = async (question: string, kind?: AskKind) => {
    if (busy) return
    push({ role: 'user', text: question })
    const cfg = loadSettings()

    // 没配置模型时退回本地演示答案
    if (!isConfigured(cfg)) {
      setBusy(true)
      window.setTimeout(() => {
        push({
          role: 'ai',
          text: kind
            ? buildAnswer(kind, paper)
            : '还没有配置模型，暂时无法检索论文回答。\n点右上角齿轮填入模型与密钥即可开始问答。',
        })
        setBusy(false)
      }, 300)
      return
    }

    setBusy(true)
    try {
      const res = await askPaper(
        paperId,
        { base_url: cfg.baseUrl, api_key: cfg.apiKey, model: cfg.model },
        question,
        useWeb,
      )
      push({
        role: 'ai',
        text: res.answer,
        sources: res.sources,
        usedWeb: res.usedWeb,
        latency: res.latency,
      })
    } catch (e) {
      push({
        role: 'ai',
        text: `回答失败：${e instanceof Error ? e.message : '未知错误'}`,
        error: true,
      })
    } finally {
      setBusy(false)
    }
  }

  const submit = () => {
    const q = input.trim()
    if (!q) return
    setInput('')
    void run(q)
  }

  return (
    <div className="chat">
      <div className="chat-list" ref={listRef}>
        {messages.length === 0 && (
          <>
            <p className="greeting">Hi，准备好阅读了吗？你想从哪里开始？</p>
            <div className="quick-label">
              <IconSpark /> 快速开始
            </div>
            <div className="chips">
              {QUICK.map((q) => (
                <button
                  key={q.key}
                  className="chip"
                  onClick={() => void run(q.label, q.key)}
                >
                  {q.label}
                </button>
              ))}
            </div>
            {!isConfigured(loadSettings()) && (
              <button className="chat-setup" onClick={onOpenSettings}>
                先配置模型，才能基于论文回答 →
              </button>
            )}
          </>
        )}

        {messages.map((m) => (
          <div key={m.id} className={`msg ${m.role}${m.error ? ' is-error' : ''}`}>
            {m.text}
            {m.sources && m.sources.length > 0 && (
              <div className="msg-sources">
                {m.sources.slice(0, 8).map((s, i) =>
                  s.type === 'web' ? (
                    <a
                      key={`w-${i}`}
                      className="src-chip src-web"
                      href={s.url}
                      target="_blank"
                      rel="noreferrer"
                      title={s.title}
                    >
                      {s.label} 网页
                    </a>
                  ) : s.type === 'ref' ? (
                    <span className="src-chip src-ref" key={`r-${i}`} title={s.title}>
                      {s.label} 文献
                    </span>
                  ) : (
                    <button
                      key={`p-${i}`}
                      className="src-chip src-paper"
                      onClick={() => s.blockId && onJump?.(s.blockId)}
                      title={s.snippet}
                    >
                      {s.label}
                    </button>
                  ),
                )}
              </div>
            )}
          </div>
        ))}

        {busy && <div className="msg ai pending">检索论文并思考中…</div>}
      </div>

      <div className="composer">
        <div className="composer-box">
          <textarea
            rows={1}
            value={input}
            placeholder="问问这篇论文"
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                submit()
              }
            }}
          />
          <button className="send" disabled={!input.trim() || busy} onClick={submit} title="发送">
            <IconSend />
          </button>
        </div>
        <label className="web-toggle">
          <input
            type="checkbox"
            checked={useWeb}
            onChange={(e) => setUseWeb(e.target.checked)}
          />
          允许联网检索
        </label>
      </div>
    </div>
  )
}
