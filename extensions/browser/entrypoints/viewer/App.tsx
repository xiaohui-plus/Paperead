import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { PointerEvent as ReactPointerEvent } from 'react'
import { browser } from 'wxt/browser'
import * as pdfjsLib from 'pdfjs-dist'
import type { PDFDocumentProxy } from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import PdfViewer from './PdfViewer'
import {
  askSelection,
  createReadingAnnotation,
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
  const [page, setPage] = useState(1)
  const [petPos, setPetPos] = useState<{ left: number; top: number } | null>(null)
  const bytesRef = useRef<Uint8Array | null>(null)
  const ingestedRef = useRef(false)
  const petWrapRef = useRef<HTMLDivElement>(null)
  const petDrag = useRef<{
    pointerId: number
    startX: number
    startY: number
    left: number
    top: number
    moved: boolean
  } | null>(null)
  const petSuppressClick = useRef(false)

  const load = useCallback(async () => {
    if (!fileUrl) {
      setError('缺少 PDF 地址')
      setPhase('error')
      return
    }
    setPhase('loading')
    setError('')
    try {
      // manifest 已声明全站 host 权限，不再用 permissions.contains 预判：
      // 某些浏览器会把 contains 判成 false，导致直接掉进权限死页。
      const response = await fetch(fileUrl, { credentials: 'include' })
      if (!response.ok) {
        if (response.status === 401 || response.status === 403) {
          setPhase('permission')
          return
        }
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

  const openReaderTab = useCallback(() => {
    if (readerUrl) window.open(readerUrl, '_blank', 'noopener')
  }, [readerUrl])

  // ── 灵宠拖动：位置写 localStorage，拖动超过 4px 抑制这次点击 ──
  useEffect(() => {
    try {
      const raw = localStorage.getItem('paperead.pet.viewer')
      if (!raw) return
      const saved = JSON.parse(raw) as { left?: number; top?: number }
      if (typeof saved.left === 'number' && typeof saved.top === 'number') {
        setPetPos({ left: saved.left, top: saved.top })
      }
    } catch {
      // 本地存储不可用时使用默认右下角
    }
  }, [])

  useEffect(() => {
    const onMove = (event: PointerEvent) => {
      const drag = petDrag.current
      if (!drag || event.pointerId !== drag.pointerId) return
      const dx = event.clientX - drag.startX
      const dy = event.clientY - drag.startY
      if (!drag.moved && Math.hypot(dx, dy) < 4) return
      drag.moved = true
      setPetPos({
        left: Math.min(Math.max(6, drag.left + dx), Math.max(6, window.innerWidth - 54)),
        top: Math.min(Math.max(6, drag.top + dy), Math.max(6, window.innerHeight - 54)),
      })
    }
    const onUp = (event: PointerEvent) => {
      const drag = petDrag.current
      if (!drag || event.pointerId !== drag.pointerId) return
      petDrag.current = null
      if (!drag.moved) return
      petSuppressClick.current = true
      const left = Math.min(
        Math.max(6, drag.left + (event.clientX - drag.startX)),
        Math.max(6, window.innerWidth - 54),
      )
      const top = Math.min(
        Math.max(6, drag.top + (event.clientY - drag.startY)),
        Math.max(6, window.innerHeight - 54),
      )
      try {
        localStorage.setItem('paperead.pet.viewer', JSON.stringify({ left, top }))
      } catch {
        // 只保留本次位置
      }
    }
    document.addEventListener('pointermove', onMove)
    document.addEventListener('pointerup', onUp)
    document.addEventListener('pointercancel', onUp)
    return () => {
      document.removeEventListener('pointermove', onMove)
      document.removeEventListener('pointerup', onUp)
      document.removeEventListener('pointercancel', onUp)
    }
  }, [])

  const onPetPointerDown = (event: ReactPointerEvent<HTMLButtonElement>) => {
    if (event.button !== 0) return
    const rect = petWrapRef.current?.getBoundingClientRect()
    if (!rect) return
    petDrag.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      left: rect.left,
      top: rect.top,
      moved: false,
    }
    try {
      event.currentTarget.setPointerCapture(event.pointerId)
    } catch {
      // 不支持 pointer capture 时仍可在 document 上继续拖
    }
    event.preventDefault()
  }

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

  const highlight = useCallback(async () => {
    if (!selection) return
    if (!paperId) {
      setPopover({
        kind: 'ask',
        title: '高亮',
        body: '论文还在解析，完成后即可保存高亮。',
        loading: false,
      })
      return
    }
    try {
      await createReadingAnnotation(paperId, selection.text, '', '', page)
      try {
        // 先给当前选区一个即时视觉反馈；持久高亮在完整阅读页按批注渲染
        document.execCommand('hiliteColor', false, 'rgba(255, 214, 102, 0.45)')
      } catch {
        // 浏览器不支持 execCommand 时只保存批注
      }
      setPopover({
        kind: 'ask',
        title: '高亮已保存',
        body: '已写入论文批注栏，可在完整阅读里查看。',
        loading: false,
      })
    } catch (cause) {
      setPopover({
        kind: 'ask',
        title: '高亮失败',
        body: cause instanceof Error ? cause.message : '请稍后重试',
        loading: false,
      })
    }
  }, [selection, paperId, page])

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
          <h1>这个 PDF 需要站点登录或验证</h1>
          <p>
            浏览器不允许扩展直接读取需要登录的 PDF。可以在浏览器里打开一次完成登录，
            或下载后拖入 Paperead。
          </p>
          <button className="button primary" onClick={() => window.open(fileUrl, '_blank', 'noopener')}>
            在浏览器中打开
          </button>
          <button className="button" onClick={() => void load()}>
            重试
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
          onPageChange={setPage}
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
          <button onClick={() => void highlight()}>高亮</button>
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

      <div
        className={`pe-pet-wrap${petOpen ? ' open' : ''}`}
        ref={petWrapRef}
        style={
          petPos
            ? { left: petPos.left, top: petPos.top, right: 'auto', bottom: 'auto' }
            : undefined
        }
      >
        <div className="pe-pet-panel" role="group" aria-label="Paperead 快捷操作">
          <button
            className="pe-act"
            type="button"
            disabled={!paperId}
            onClick={() => setMode('reader')}
          >
            使用 Paperead 在线阅读
          </button>
          <button
            className="pe-act"
            type="button"
            disabled={!readerUrl}
            onClick={() => void openReaderTab()}
          >
            在 Paperead 本地阅读
          </button>
          <button
            className="pe-act"
            type="button"
            disabled={!bytesRef.current || paperReady}
            onClick={() => void runIngest()}
          >
            {paperReady ? '已保存到 Paperead' : '保存到 Paperead'}
          </button>
          <button
            className="pe-act"
            type="button"
            disabled={petBusy}
            onClick={() => void openAssistant()}
          >
            打开阅读助手
          </button>
          {petMsg && <p className="pe-msg">{petMsg}</p>}
        </div>
        <button
          className="pe-pet"
          type="button"
          aria-label="Paperead 灵宠"
          aria-expanded={petOpen}
          onPointerDown={onPetPointerDown}
          onClick={() => {
            if (petSuppressClick.current) {
              petSuppressClick.current = false
              return
            }
            setPetOpen((value) => !value)
          }}
        >
          <span className="pe-eye" />
          <span className="pe-eye" />
          <span className="pe-dot" />
        </button>
      </div>
    </div>
  )
}