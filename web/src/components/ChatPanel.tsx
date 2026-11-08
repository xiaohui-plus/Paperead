import { useEffect, useMemo, useRef, useState } from 'react'
import type { Paper } from '../types'
import { askPaperStream, type AskSource } from '../lib/library'
import {
  isModelReady,
  loadModelSettings,
  readyModel,
  saveModelSettings,
  toPayload,
  type ModelSettings,
} from '../lib/settings'
import { buildAnswer, type AskKind } from '../lib/assistant'
import { Markdown, balanceMarkdown } from './Markdown'
import { IconCheck, IconSend, IconSettings, IconSpark, IconStop } from './icons'

type TurnStatus = 'retrieving' | 'streaming' | 'done' | 'error' | 'stopped'

interface Turn {
  id: number
  question: string
  answer: string
  sources: AskSource[]
  usedWeb: boolean
  latency?: number
  status: TurnStatus
  error?: string
  /** 本地演示答案：不是模型生成，按纯文本渲染，保留换行 */
  demo?: boolean
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
  settingsVersion = 0,
}: {
  paper: Paper
  paperId: string
  onJump?: (id: string) => void
  onOpenSettings?: () => void
  settingsVersion?: number
}) {
  const [turns, setTurns] = useState<Turn[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [useWeb, setUseWeb] = useState(true)
  const [settings, setSettings] = useState<ModelSettings>(loadModelSettings)
  const [pickerOpen, setPickerOpen] = useState(false)
  const listRef = useRef<HTMLDivElement>(null)
  const pickerRef = useRef<HTMLDivElement>(null)
  const seq = useRef(0)
  const busyRef = useRef(false)
  const stickRef = useRef(true)
  const abortRef = useRef<AbortController | null>(null)

  const modelOptions = useMemo(
    () => settings.models.filter((m) => m.kind === 'answer' && isModelReady(m)),
    [settings],
  )
  const current = useMemo(() => readyModel(settings, 'answer'), [settings])

  const patch = (id: number, fn: (t: Turn) => Turn) =>
    setTurns((prev) => prev.map((t) => (t.id === id ? fn(t) : t)))

  // 只在用户没有主动上翻时贴底，避免流式回答把人从正在读的位置拽走
  useEffect(() => {
    const el = listRef.current
    if (!el || !stickRef.current) return
    el.scrollTop = el.scrollHeight
  }, [turns, busy])

  // 设置弹窗保存后刷新可选模型
  useEffect(() => {
    if (!settingsVersion) return
    setSettings(loadModelSettings())
  }, [settingsVersion])

  useEffect(() => {
    if (!pickerOpen) return
    const onDown = (e: MouseEvent) => {
      if (!pickerRef.current?.contains(e.target as Node)) setPickerOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [pickerOpen])

  useEffect(() => () => abortRef.current?.abort(), [])

  const onScroll = () => {
    const el = listRef.current
    if (!el) return
    stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48
  }

  const chooseModel = (id: string) => {
    const next = { ...settings, active: { ...settings.active, answer: id } }
    saveModelSettings(next)
    setSettings(next)
    setPickerOpen(false)
  }

  const ask = async (question: string, kind?: AskKind) => {
    if (busyRef.current) return
    busyRef.current = true
    setBusy(true)
    stickRef.current = true

    seq.current += 1
    const id = seq.current
    const cfg = readyModel(loadModelSettings(), 'answer')

    setTurns((prev) => [
      ...prev,
      {
        id,
        question,
        answer: '',
        sources: [],
        usedWeb: false,
        status: 'retrieving',
        demo: !cfg,
      },
    ])

    // 没配置回答模型时退回本地演示答案
    if (!cfg) {
      const text = kind
        ? buildAnswer(kind, paper)
        : '还没有配置回答模型，暂时无法检索论文回答。\n点下面的「配置回答模型」填入模型与密钥即可开始问答。'
      window.setTimeout(() => {
        patch(id, (t) => ({ ...t, answer: text, status: 'done' }))
        busyRef.current = false
        setBusy(false)
      }, 250)
      return
    }

    const controller = new AbortController()
    abortRef.current = controller

    // 增量先攒在 buffer 里，每 50ms 合并一次 setState：
    // 既能逐字出现，又不会因为每个 token 重渲染整段 Markdown 而抖动。
    let buffer = ''
    let timer: number | null = null
    let streamError = ''
    let gotDelta = false

    const flush = () => {
      if (timer !== null) {
        window.clearTimeout(timer)
        timer = null
      }
      if (!buffer) return
      const chunk = buffer
      buffer = ''
      patch(id, (t) => ({ ...t, answer: t.answer + chunk }))
    }

    const onDelta = (chunk: string) => {
      buffer += chunk
      if (!gotDelta) {
        gotDelta = true
        patch(id, (t) => ({ ...t, status: 'streaming' }))
      }
      if (timer === null) {
        timer = window.setTimeout(() => {
          timer = null
          flush()
        }, 50)
      }
    }

    try {
      await askPaperStream(
        paperId,
        toPayload(cfg),
        question,
        useWeb,
        (event) => {
          switch (event.type) {
            case 'sources':
              patch(id, (t) => ({
                ...t,
                sources: event.sources ?? [],
                usedWeb: Boolean(event.usedWeb),
                status: t.answer ? t.status : 'streaming',
              }))
              break
            case 'delta':
              if (event.text) onDelta(event.text)
              break
            case 'done':
              flush()
              patch(id, (t) => ({ ...t, status: 'done', latency: event.latency }))
              break
            case 'error':
              streamError = event.message || '回答失败'
              break
            default:
              break
          }
        },
        controller.signal,
      )

      if (streamError) throw new Error(streamError)

      // 服务端没发 done 就断流时，把已收到的内容当作完成
      flush()
      patch(id, (t) => (t.status === 'done' ? t : { ...t, status: 'done' }))
    } catch (e) {
      flush()
      if (controller.signal.aborted) {
        patch(id, (t) => ({ ...t, status: 'stopped' }))
      } else {
        patch(id, (t) => ({
          ...t,
          status: 'error',
          error: e instanceof Error ? e.message : '未知错误',
        }))
      }
    } finally {
      if (timer !== null) window.clearTimeout(timer)
      abortRef.current = null
      busyRef.current = false
      setBusy(false)
    }
  }

  const submit = () => {
    const q = input.trim()
    if (!q || busyRef.current) return
    setInput('')
    void ask(q)
  }

  // 深链 ?ask=xxx 自动提问（也方便分享一个问题）
  const asked = useRef(false)
  useEffect(() => {
    if (asked.current) return
    const q = new URLSearchParams(window.location.search).get('ask')
    if (!q) return
    asked.current = true
    void ask(q)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div className="chat">
      <div className="chat-list" ref={listRef} onScroll={onScroll}>
        {turns.length === 0 && (
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
                  onClick={() => void ask(q.label, q.key)}
                >
                  {q.label}
                </button>
              ))}
            </div>
            {!current && (
              <button className="chat-setup" onClick={onOpenSettings}>
                先配置回答模型，才能基于论文回答 →
              </button>
            )}
          </>
        )}

        {turns.map((t) => (
          <article className="turn" key={t.id}>
            <div className="turn-q">{t.question}</div>

            <div className="turn-a">
              {t.answer &&
                (t.demo ? (
                  <div className="turn-plain">{t.answer}</div>
                ) : (
                  <Markdown
                    text={
                      t.status === 'streaming'
                        ? `${balanceMarkdown(t.answer)} ▍`
                        : t.status === 'stopped'
                          ? balanceMarkdown(t.answer)
                          : t.answer
                    }
                  />
                ))}

              {(t.status === 'retrieving' || t.status === 'streaming') &&
                !t.answer && (
                  <div className="turn-status">
                    <span className="turn-dot" />
                    {t.status === 'retrieving' ? '检索论文并思考中…' : '正在生成回答…'}
                  </div>
                )}

              {t.sources.length > 0 && (
                <div className="turn-sources">
                  <span className="turn-sources-label">出处</span>
                  {t.sources.slice(0, 8).map((s, i) =>
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

              {t.status === 'error' && (
                <div className="turn-error">
                  {t.error ? `回答中断：${t.error}` : '回答中断'}
                </div>
              )}

              {t.status === 'stopped' && <div className="turn-note">已停止生成</div>}

              {t.status === 'done' && t.latency !== undefined && (
                <div className="turn-meta">
                  用时 {t.latency.toFixed(1)}s
                  {t.usedWeb ? ' · 已联网检索' : ''}
                </div>
              )}
            </div>
          </article>
        ))}
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
          {busy ? (
            <button
              className="send stop"
              onClick={() => abortRef.current?.abort()}
              title="停止生成"
            >
              <IconStop />
            </button>
          ) : (
            <button className="send" disabled={!input.trim()} onClick={submit} title="发送">
              <IconSend />
            </button>
          )}
        </div>
        <div className="composer-meta">
          <div className="model-picker" ref={pickerRef}>
            {current ? (
              <>
                <button
                  className="model-select"
                  onClick={() => setPickerOpen((v) => !v)}
                  title="切换回答模型"
                  aria-haspopup="menu"
                  aria-expanded={pickerOpen}
                >
                  <span className="model-select-dot" />
                  <span className="model-select-name">{current.label}</span>
                  <span className="model-select-caret" aria-hidden>
                    ▾
                  </span>
                </button>
                {pickerOpen && (
                  <div className="model-menu" role="menu">
                    <div className="model-menu-head">回答模型</div>
                    {modelOptions.map((m) => (
                      <button
                        key={m.id}
                        className="model-menu-item"
                        data-active={m.id === current.id}
                        role="menuitem"
                        onClick={() => chooseModel(m.id)}
                      >
                        <span className="model-menu-main">
                          <b>{m.label}</b>
                          <code>{m.model}</code>
                        </span>
                        {m.id === current.id && <IconCheck />}
                      </button>
                    ))}
                    <button
                      className="model-menu-manage"
                      onClick={() => {
                        setPickerOpen(false)
                        onOpenSettings?.()
                      }}
                    >
                      <IconSettings /> 管理模型…
                    </button>
                  </div>
                )}
              </>
            ) : (
              <button className="model-select is-empty" onClick={onOpenSettings}>
                <IconSpark /> 配置回答模型
              </button>
            )}
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
    </div>
  )
}
