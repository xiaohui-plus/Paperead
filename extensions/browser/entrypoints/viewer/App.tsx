import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { browser } from 'wxt/browser'
import * as pdfjsLib from 'pdfjs-dist'
import type { PDFDocumentProxy } from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import PdfViewer from './PdfViewer'
import {
  askSelection,
  discoverBase,
  getPaper,
  ingest,
  readingStatus,
  translateSelection,
} from '../../lib/api'
import type { PaperMetadata } from '../../lib/types'

pdfjsLib.GlobalWorkerOptions.workerSrc = workerUrl

type Phase = 'loading' | 'permission' | 'ready' | 'error'
type Mode = 'pdf' | 'reader'

interface Selection {
  text: string
  left: number
  top: number
  bottom: number
}

interface Popover {
  kind: 'translate' | 'ask'
  title: string
  body: string
  loading: boolean
}

const isPdfBytes = (bytes: Uint8Array) =>
  bytes.length > 4 &&
  bytes[0] === 0x25 &&
  bytes[1] === 0x50 &&
  bytes[2] === 0x44 &&
  bytes[3] === 0x46

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

export default function App() {
  const fileUrl = useMemo(() => {
    const value = new URLSearchParams(location.search).get('file') || ''
    return value
  }, [])
  const filename = useMemo(() => {
    try {
      const name = decodeURIComponent(new URL(fileUrl).pathname.split('/').pop() || '')
      return name || 'paper.pdf'
    } catch {
      return 'paper.pdf'
    }
  }, [fileUrl])

  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState('')
  const [doc, setDoc] = useState<PDFDocumentProxy | null>(null)
  const [status, setStatus] = useState('')
  const [paperId, setPaperId] = useState('')
  const [paperReady, setPaperReady] = useState(false)
  const [modelLabel, setModelLabel] = useState('')
  const [mode, setMode] = useState<Mode>('pdf')
  const [readerUrl, setReaderUrl] = useState('')
  const [readerKey, setReaderKey] = useState(0)
  const [selection, setSelection] = useState<Selection | null>(null)
  const [popover, setPopover] = useState<Popover | null>(null)
  const [petOpen, setPetOpen] = useState(false)
  const [petMsg, setPetMsg] = useState('')
  const [petBusy, setPetBusy] = useState(false)
  const bytesRef = useRef<Uint8Array | null>(null)
  const ingestedRef = useRef(false)

  const load = useCallback(async () => {
    if (!fileUrl) {
      setError('缺少 PDF 地址')
      setPhase('error')
      return
    }
    setPhase('loading')
    setError('')
    try {
      const origin = `${new URL(fileUrl).origin}/*`
      const granted = await browser.permissions.contains({ origins: [origin] })
      if (!granted) {
        setPhase('permission')
        return
      }
      const response = await fetch(fileUrl, { credentials: 'include' })
      if (!response.ok) {
        throw new Error(`无法读取 PDF（HTTP ${response.status}），可能需要登录该站点`)
      }
      const bytes = new Uint8Array(await response.arrayBuffer())
      if (!isPdfBytes(bytes)) {
        throw new Error('返回的内容不是 PDF，可能需要登录或通过验证')
      }
      bytesRef.current = bytes
      const loaded = await pdfjsLib.getDocument({ data: new Uint8Array(bytes) }).promise
      setDoc(loaded)
      setPhase('ready')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'PDF 加载失败')
      setPhase('error')
    }
  }, [fileUrl])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    void readingStatus()
      .then((data) => setModelLabel(data.model_ready ? data.model_label : ''))
      .catch(() => setModelLabel(''))
  }, [])

  const requestPermission = useCallback(async () => {
    try {
      const origin = `${new URL(fileUrl).origin}/*`
      const granted = await browser.permissions.request({ origins: [origin] })
      if (granted) {
        await load()
      } else {
        setError('未授予该站点的读取权限，无法获取 PDF')
        setPhase('error')
      }
    } catch {
      setError('申请站点权限失败，请重试')
      setPhase('error')
    }
  }, [fileUrl, load])

  const runIngest = useCallback(async () => {
    const bytes = bytesRef.current
    if (!bytes || ingestedRef.current) return
    ingestedRef.current = true
    setStatus('正在存入 Paperead…')
    try {
      const meta: PaperMetadata = {
        source: 'pdf',
        source_id: '',
        title: filename.replace(/\.pdf$/i, ''),
        authors: '',
        abstract: '',
        doi: '',
        url: fileUrl,
        year: null,
        venue: '',
        pdf_url: fileUrl,
      }
      const saved = await ingest(bytes, meta)
      setPaperId(saved.paper.id)
      try {
        // 供侧边栏按当前 viewer 标签页的 file 参数找回 paper id
        await browser.storage.session.set({
          [`viewerPaper:${fileUrl}`]: saved.paper.id,
        })
      } catch {
        // 不支持 storage.session 的环境忽略，阅读页本身不受影响
      }
      setStatus(saved.already ? '已在文献库中，正在确认解析状态…' : '正在解析 PDF…')
      for (let attempt = 0; attempt < 120; attempt += 1) {
        const paper = await getPaper(saved.paper.id)
        if (paper.status === 'ready') {
          setPaperReady(true)
          setStatus('已存入文献库')
          return
        }
        if (paper.status === 'failed') {
          setStatus('解析失败，可在 Paperead 文献库中重试')
          return
        }
        await sleep(1500)
      }
      setStatus('解析时间较长，可稍后到文献库查看')
    } catch (cause) {
      ingestedRef.current = false
      setStatus(cause instanceof Error ? cause.message : '存入失败')
    }
  }, [fileUrl, filename])

  useEffect(() => {
    if (doc) void runIngest()
  }, [doc, runIngest])

  useEffect(() => {
    if (!paperId) {
      setReaderUrl('')
      return
    }
    let cancelled = false
    void discoverBase()
      .then((base) => {
        if (!cancelled) setReaderUrl(`${base}/?p=${paperId}`)
      })
      .catch(() => {
        if (!cancelled) setReaderUrl('')
      })
    return () => {
      cancelled = true
    }
  }, [paperId])

  // 解析完成前 iframe 里可能只看到“解析中”。解析完成时强制重载一次，
  // 让完整阅读直接用最终的 blocks.json 渲染。
  useEffect(() => {
    if (paperReady) setReaderKey((value) => value + 1)
  }, [paperReady])

  useEffect(() => {
    const onMouseUp = (event: MouseEvent) => {
      const target = event.target as HTMLElement | null
      if (target?.closest('.popover, .selbar, .pdf-toolbar, .pdf-search')) return
      window.setTimeout(() => {
        const current = window.getSelection()
        const text = current?.toString().trim() || ''
        if (!current || current.rangeCount === 0 || text.length < 2) {
          setSelection(null)
          return
        }
        const rect = current.getRangeAt(0).getBoundingClientRect()
        if (!rect.width && !rect.height) {
          setSelection(null)
          return
        }
        setSelection({
          text: text.slice(0, 4000),
          left: Math.min(Math.max(rect.left + rect.width / 2, 100), window.innerWidth - 100),
          top: Math.max(rect.top - 8, 56),
          bottom: rect.bottom,
        })
      }, 0)
    }
    document.addEventListener('mouseup', onMouseUp)
    return () => document.removeEventListener('mouseup', onMouseUp)
  }, [])

  const openPaper = useCallback(async () => {
    if (!paperId) return
    if (readerUrl) {
      window.open(readerUrl, '_blank', 'noopener')
      return
    }
    const base = await discoverBase()
    window.open(`${base}/?p=${paperId}`, '_blank', 'noopener')
  }, [paperId, readerUrl])

  const openReaderTab = useCallback(() => {
    if (readerUrl) window.open(readerUrl, '_blank', 'noopener')
  }, [readerUrl])

  const openAssistant = useCallback(async () => {
    setPetBusy(true)
    setPetMsg('')
    try {
      const result = (await browser.runtime.sendMessage({ type: 'open-assistant' })) as
        | { ok?: boolean }
        | undefined
      if (!result?.ok) setPetMsg('请点工具栏的 P 图标打开阅读助手')
    } catch {
      setPetMsg('请点工具栏的 P 图标打开阅读助手')
    } finally {
      setPetBusy(false)
    }
  }, [])

  const translate = useCallback(async () => {
    if (!selection) return
    setPopover({ kind: 'translate', title: '翻译', body: '正在翻译…', loading: true })
    try {
      const result = await translateSelection(selection.text)
      setPopover({
        kind: 'translate',
        title: `翻译 · ${result.target_name || '简体中文'}`,
        body: result.translation,
        loading: false,
      })
    } catch (cause) {
      setPopover({
        kind: 'translate',
        title: '翻译失败',
        body: cause instanceof Error ? cause.message : '请稍后重试',
        loading: false,
      })
    }
  }, [selection])

  const ask = useCallback(async () => {
    if (!selection) return
    if (!paperId || !paperReady) {
      setPopover({
        kind: 'ask',
        title: '提问',
        body: '这篇 PDF 还在解析，请稍候再试。解析完成后会自动就绪。',
        loading: false,
      })
      return
    }
    setPopover({ kind: 'ask', title: '提问', body: '正在结合论文上下文回答…', loading: true })
    try {
      const result = await askSelection(
        paperId,
        `请结合论文内容解释这段话，并指出它在论证中的作用：\n\n${selection.text}`,
      )
      setPopover({ kind: 'ask', title: '基于论文的回答', body: result.answer, loading: false })
    } catch (cause) {
      setPopover({
        kind: 'ask',
        title: '提问失败',
        body: cause instanceof Error ? cause.message : '请稍后重试',
        loading: false,
      })
    }
  }, [selection, paperId, paperReady])

  const readerStatus = paperReady
    ? '完整阅读已就绪'
    : paperId
      ? '解析中，完成后自动刷新'
      : '正在准备…'

  return (
    <div className="viewer">
      <div className="topbar">
        <span className="brand">Paperead</span>
        <span className="filename" title={filename}>
          {filename}
        </span>
        <span className="status">
          {status || (modelLabel ? `模型 ${modelLabel}` : '未配置模型')}
        </span>
        <div className="mode-switch" role="group" aria-label="阅读模式">
          <button
            className={`mode${mode === 'pdf' ? ' active' : ''}`}
            type="button"
            aria-pressed={mode === 'pdf'}
            onClick={() => setMode('pdf')}
          >
            PDF 原版
          </button>
          <button
            className={`mode${mode === 'reader' ? ' active' : ''}`}
            type="button"
            aria-pressed={mode === 'reader'}
            disabled={!paperId}
            title={
              paperId
                ? '显示 Paperead 解析后的完整阅读视图（翻译、图表、问答、标注）'
                : '解析开始后即可进入完整阅读'
            }
            onClick={() => setMode('reader')}
          >
            完整阅读
          </button>
        </div>
      </div>

      {phase === 'loading' && (
        <div className="center">
          <h1>正在打开 PDF</h1>
          <p>从原站点读取文件，不会上传到第三方。</p>
        </div>
      )}

      {phase === 'permission' && (
        <div className="center">
          <h1>需要访问该站点的权限</h1>
          <p>Paperead 只读取这一个 PDF，用于本地渲染、翻译和问答。</p>
          <button className="button primary" onClick={() => void requestPermission()}>
            允许读取并打开
          </button>
        </div>
      )}

      {phase === 'error' && (
        <div className="center">
          <h1>无法打开这个 PDF</h1>
          <p>{error}</p>
          <button className="button" onClick={() => window.location.reload()}>
            重试
          </button>
        </div>
      )}

      {phase === 'ready' && doc && mode === 'pdf' && (
        <PdfViewer
          doc={doc}
          bytes={bytesRef.current}
          filename={filename}
          fileUrl={fileUrl}
        />
      )}

      {mode === 'reader' && paperId && (
        <div className="reader-pane">
          <div className="reader-bar">
            <span className={paperReady ? 'reader-ready' : 'reader-wait'}>{readerStatus}</span>
            <button type="button" className="button" onClick={openReaderTab} disabled={!readerUrl}>
              在新标签页打开
            </button>
            <button
              type="button"
              className="button"
              onClick={() => setReaderKey((value) => value + 1)}
              disabled={!readerUrl}
            >
              刷新
            </button>
            <button type="button" className="button" onClick={() => setMode('pdf')}>
              返回 PDF 原版
            </button>
          </div>
          {readerUrl ? (
            <iframe
              key={readerKey}
              className="reader-frame"
              src={readerUrl}
              title="Paperead 完整阅读"
            />
          ) : (
            <div className="center">
              <h1>正在准备完整阅读…</h1>
              <p>请确认桌面端 Paperead 正在运行。</p>
            </div>
          )}
        </div>
      )}

      {selection && mode === 'pdf' && (
        <div
          className="selbar"
          style={{ left: selection.left, top: selection.top }}
          onMouseDown={(event) => event.preventDefault()}
        >
          <button onClick={() => void translate()}>翻译</button>
          <button onClick={() => void ask()}>提问</button>
        </div>
      )}

      {popover && (
        <div
          className="popover"
          style={{
            left: Math.min(Math.max((selection?.left || 100) - 170, 12), window.innerWidth - 352),
            top: Math.min(Math.max((selection?.bottom || 120) + 10, 64), window.innerHeight - 220),
          }}
        >
          <header>
            <span>{popover.title}</span>
            <span>
              {!popover.loading && (
                <button
                  onClick={() => {
                    void navigator.clipboard.writeText(popover.body)
                  }}
                >
                  复制
                </button>
              )}
              <button onClick={() => setPopover(null)}>关闭</button>
            </span>
          </header>
          <div className={`body${popover.loading ? ' muted' : ''}`}>{popover.body}</div>
        </div>
      )}

      <div className={`pe-pet-wrap${petOpen ? ' open' : ''}`}>
        <div className="pe-pet-panel" role="group" aria-label="Paperead 快捷操作">
          <button
            className="pe-act"
            type="button"
            disabled={!paperId}
            onClick={() => void openPaper()}
          >
            {paperReady ? '打开 Paperead' : '正在解析…'}
          </button>
          <button className="pe-act" type="button" disabled={petBusy} onClick={() => void openAssistant()}>
            打开阅读助手
          </button>
          {petMsg && <p className="pe-msg">{petMsg}</p>}
        </div>
        <button
          className="pe-pet"
          type="button"
          aria-label="Paperead 灵宠"
          aria-expanded={petOpen}
          onClick={() => setPetOpen((value) => !value)}
        >
          <span className="pe-eye" />
          <span className="pe-eye" />
          <span className="pe-dot" />
        </button>
      </div>
    </div>
  )
}