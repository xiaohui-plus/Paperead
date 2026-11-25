import { useCallback, useEffect, useState } from 'react'
import { IconCheck, IconDownload, IconRefresh } from './icons'

interface ToolInfo {
  found: boolean
  path: string
  version: string
}

interface ToolchainStatus {
  pandoc: ToolInfo
  pandocDownload: {
    status: 'idle' | 'running' | 'done' | 'error'
    percent: number
    error: string
    installed: boolean
    version: string
  }
  nodeDownload: {
    status: 'idle' | 'running' | 'done' | 'error'
    percent: number
    error: string
    installed: boolean
    version: string
  }
  engines: { name: string; path: string; version: string }[]
  bibtex: ToolInfo
  /** LaTeX 引擎（MiKTeX 便携版）自动安装：下载 → 安装 → 配置 → 中文支持 */
  texDownload?: {
    status: 'idle' | 'running' | 'done' | 'error'
    phase: string
    percent: number
    error: string
    warning: string
    installed: boolean
    version: string
    bin: string
  }
  /** 全局翻译引擎（BabelDOC）：独立于 PDF 导出链，需单独下载 */
  engineDownload?: {
    status: 'idle' | 'running' | 'done' | 'error'
    percent: number
    error: string
    installed: boolean
    ready: boolean
    version: string
  }
  installer: { winget: boolean; package: string }
  ready: boolean
  missing: string[]
  install: { running: boolean; jobId: string | null }
}

interface InstallJob {
  id: string
  status: 'running' | 'done' | 'error'
  log: string[]
  exitCode: number | null
  error: string | null
}

const DOWNLOADS = [
  { label: 'MiKTeX（推荐，可按需下载宏包）', url: 'https://miktex.org/download' },
  { label: 'TeX Live（完整，体积较大）', url: 'https://tug.org/texlive/windows.html' },
]

const WINGET_COMMAND = 'winget install --id MiKTeX.MiKTeX -e'

const TEX_PHASE: Record<string, string> = {
  download: '下载安装程序',
  packages: '下载 LaTeX 宏包',
  install: '安装引擎',
  configure: '配置自动补包',
  extras: '安装中文支持',
  done: '完成',
}

function StatusPill({ ok, text }: { ok: boolean; text: string }) {
  return (
    <span className="toolchain-pill" data-ok={ok}>
      {ok ? <IconCheck /> : null}
      {text}
    </span>
  )
}

function ToolRow({
  name,
  hint,
  ok,
  detail,
}: {
  name: string
  hint: string
  ok: boolean
  detail: string
}) {
  return (
    <div className="toolchain-row">
      <div className="toolchain-row-main">
        <b>{name}</b>
        <span>{hint}</span>
        {detail && <code>{detail}</code>}
      </div>
      <StatusPill ok={ok} text={ok ? '已就绪' : '未检测到'} />
    </div>
  )
}

/** 设置 → 写作工具：检测 PDF 导出所需的 pandoc / TeX 引擎，并提供快捷安装。 */
export function ToolchainPanel() {
  const [status, setStatus] = useState<ToolchainStatus | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [starting, setStarting] = useState(false)
  const [job, setJob] = useState<InstallJob | null>(null)
  const [copied, setCopied] = useState(false)
  const [startingPandoc, setStartingPandoc] = useState(false)
  const [startingNode, setStartingNode] = useState(false)
  const [startingEngine, setStartingEngine] = useState(false)
  const [startingTex, setStartingTex] = useState(false)

  const load = useCallback(async (silent = false, refresh = false) => {
    if (!silent) setLoading(true)
    try {
      const res = await fetch(`/api/toolchain${refresh ? '?refresh=1' : ''}`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      setStatus((await res.json()) as ToolchainStatus)
      setError('')
    } catch (e) {
      setError(e instanceof Error ? e.message : '检测失败')
    } finally {
      if (!silent) setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  // pandoc 与公式引擎的 node 运行时都是首次启动在后台下载，这里跟着进度刷新，
  // 下载完自动变成“已就绪”。
  const pandocState = status?.pandocDownload?.status
  const nodeState = status?.nodeDownload?.status
  const engineState = status?.engineDownload?.status
  const texState = status?.texDownload?.status
  useEffect(() => {
    if (
      pandocState !== 'running' &&
      nodeState !== 'running' &&
      engineState !== 'running' &&
      texState !== 'running'
    ) {
      return
    }
    const timer = window.setInterval(() => void load(true), 2000)
    return () => window.clearInterval(timer)
  }, [pandocState, nodeState, engineState, texState, load])

  // 安装是后台任务：轮询到结束再重新检测一次，用户能看到引擎从“未检测到”变成“已就绪”。
  useEffect(() => {
    if (!job || job.status !== 'running') return
    let alive = true
    const timer = window.setInterval(async () => {
      try {
        const res = await fetch(`/api/toolchain/install/${job.id}`)
        if (!res.ok) return
        const data = (await res.json()) as InstallJob
        if (!alive) return
        setJob(data)
        if (data.status !== 'running') void load(false, true)
      } catch {
        /* 网络抖动不打断轮询 */
      }
    }, 2500)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [job?.id, job?.status, load])

  const startInstall = async () => {
    setStarting(true)
    setError('')
    try {
      const res = await fetch('/api/toolchain/install', { method: 'POST' })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`)
      if (data.jobId) {
        setJob({ id: data.jobId, status: 'running', log: [], exitCode: null, error: null })
      } else {
        await load(false, true)
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : '启动安装失败')
    } finally {
      setStarting(false)
    }
  }

  const copyCommand = async () => {
    try {
      await navigator.clipboard.writeText(WINGET_COMMAND)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1800)
    } catch {
      /* 浏览器不允许剪贴板时忽略 */
    }
  }

  const startPandoc = async () => {
    setStartingPandoc(true)
    setError('')
    try {
      const res = await fetch('/api/toolchain/pandoc', { method: 'POST' })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`)
      await load(true)
    } catch (e) {
      setError(e instanceof Error ? e.message : '下载 pandoc 失败')
    } finally {
      setStartingPandoc(false)
    }
  }

  const startNode = async () => {
    setStartingNode(true)
    setError('')
    try {
      const res = await fetch('/api/toolchain/node', { method: 'POST' })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`)
      await load(true)
    } catch (e) {
      setError(e instanceof Error ? e.message : '下载公式引擎组件失败')
    } finally {
      setStartingNode(false)
    }
  }

  const startEngine = async () => {
    setStartingEngine(true)
    setError('')
    try {
      const res = await fetch('/api/toolchain/engine', { method: 'POST' })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`)
      await load(true)
    } catch (e) {
      setError(e instanceof Error ? e.message : '下载翻译引擎失败')
    } finally {
      setStartingEngine(false)
    }
  }

  const startTex = async () => {
    setStartingTex(true)
    setError('')
    try {
      const res = await fetch('/api/toolchain/tex', { method: 'POST' })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`)
      await load(true)
    } catch (e) {
      setError(e instanceof Error ? e.message : '安装 LaTeX 引擎失败')
    } finally {
      setStartingTex(false)
    }
  }

  const engine = status?.engines[0]
  const installing = job?.status === 'running' || starting
  const download = status?.pandocDownload
  const downloading = download?.status === 'running'
  const nodeDownload = status?.nodeDownload
  const nodeDownloading = nodeDownload?.status === 'running'
  const engineDownload = status?.engineDownload
  // 翻译入口只看「有可用引擎」（installed）；ready 表示当前期望版本已升级到位，
  // 旧版本在时翻译照样能跑，不该在设置页误报「未就绪」。
  const engineReady = Boolean(engineDownload?.installed || engineDownload?.ready)
  const engineDownloading = engineDownload?.status === 'running'

  const texDownload = status?.texDownload
  const texDownloading = texDownload?.status === 'running'
  const texPhaseText = TEX_PHASE[texDownload?.phase ?? ''] ?? '安装中'

  return (
    <div className="modal-body toolchain-panel">
      <section className="toolchain-head">
        <div>
          <h3>写作工具</h3>
          <p>
            首次启动会自动补齐写作依赖：pandoc（约 24MB）、公式引擎组件（约 32MB）、
            全局翻译引擎（约 218MB）与 LaTeX 引擎（MiKTeX 便携版，约 250MB），只需一次。
            <br />
            装好后完全离线可用；缺宏包（含中文 ctex）会在导出时自动下载。
          </p>
        </div>
        <button
          className="btn small"
          onClick={() => void load(false, true)}
          disabled={loading}
        >
          <IconRefresh /> {loading ? '检测中…' : '重新检测'}
        </button>
      </section>

      {error && <div className="update-error">检测失败：{error}</div>}

      <div className="toolchain-list">
        <ToolRow
          name="pandoc"
          hint="Markdown 转 LaTeX"
          ok={Boolean(status?.pandoc.found)}
          detail={status?.pandoc.version || ''}
        />
        <ToolRow
          name="LaTeX 引擎"
          hint="编译 PDF（xelatex / pdflatex / lualatex）"
          ok={Boolean(engine)}
          detail={
            engine
              ? `${engine.name} · ${engine.version}`.trim()
              : texDownloading
                ? `${texPhaseText} ${texDownload?.percent ?? 0}%`
                : ''
          }
        />
        <ToolRow
          name="BibTeX"
          hint="参考文献排序与格式化"
          ok={Boolean(status?.bibtex.found)}
          detail={status?.bibtex.version || ''}
        />
        <ToolRow
          name="公式引擎"
          hint="MathJax 完整 TeX（node 运行时）"
          ok={Boolean(status?.nodeDownload?.installed)}
          detail={
            status?.nodeDownload?.installed
              ? `node ${status.nodeDownload.version}`
              : ''
          }
        />
        <ToolRow
          name="全局翻译引擎"
          hint="BabelDOC 整篇翻译（独立组件，约 218MB）"
          ok={engineReady}
          detail={
            engineReady
              ? engineDownload?.ready
                ? `v${engineDownload?.version ?? ''}`
                : '已安装'
              : engineDownloading
                ? `下载中 ${engineDownload?.percent ?? 0}%`
                : ''
          }
        />
      </div>

      {status && !status.pandoc.found && (
        <div className="toolchain-install">
          <p>
            {downloading
              ? `pandoc 正在后台下载（${download?.percent ?? 0}%），完成后自动可用。`
              : download?.status === 'error'
                ? `pandoc 下载失败：${download.error || '网络不可用'}`
                : 'pandoc 用于 DOCX / LaTeX 导出，首次使用前需要下载一次。'}
          </p>
          <div className="toolchain-actions">
            <button
              className="btn primary"
              onClick={() => void startPandoc()}
              disabled={downloading || startingPandoc}
            >
              <IconDownload />
              {downloading
                ? `正在下载 ${download?.percent ?? 0}%`
                : startingPandoc
                  ? '正在启动…'
                  : download?.status === 'error'
                    ? '重试下载 pandoc'
                    : '下载 pandoc'}
            </button>
          </div>
        </div>
      )}

      {status && !status.nodeDownload?.installed && (
        <div className="toolchain-install">
          <p>
            {nodeDownloading
              ? `公式引擎组件正在后台下载（${nodeDownload?.percent ?? 0}%），完成后自动启用。`
              : nodeDownload?.status === 'error'
                ? `公式引擎组件下载失败：${nodeDownload.error || '网络不可用'}`
                : '公式排版需要 node 运行时（约 32MB），首次启动会自动下载，之后离线可用。'}
          </p>
          <div className="toolchain-actions">
            <button
              className="btn primary"
              onClick={() => void startNode()}
              disabled={nodeDownloading || startingNode}
            >
              <IconDownload />
              {nodeDownloading
                ? `正在下载 ${nodeDownload?.percent ?? 0}%`
                : startingNode
                  ? '正在启动…'
                  : nodeDownload?.status === 'error'
                    ? '重试下载公式引擎'
                    : '下载公式引擎'}
            </button>
          </div>
        </div>
      )}

      {status && !engineReady && (
        <div className="toolchain-install">
          <p>
            {engineDownloading
              ? `全局翻译引擎正在后台下载（${engineDownload?.percent ?? 0}%），完成后「译文 / 对照」会自动开始翻译。`
              : engineDownload?.status === 'error'
                ? `全局翻译引擎下载失败：${engineDownload?.error || '网络不可用'}`
                : '整篇翻译需要首次联网下载 BabelDOC 引擎（约 218MB），之后离线可用。'}
          </p>
          {engineDownloading && (
            <div className="storage-progress">
              <div className="storage-progress-track">
                <span
                  className="storage-progress-fill"
                  style={{ width: `${Math.max(2, engineDownload?.percent ?? 0)}%` }}
                />
              </div>
              <div className="storage-progress-meta">
                <span>正在下载翻译引擎</span>
                <span>{engineDownload?.percent ?? 0}%</span>
              </div>
            </div>
          )}
          <div className="toolchain-actions">
            <button
              className="btn primary"
              onClick={() => void startEngine()}
              disabled={engineDownloading || startingEngine}
            >
              <IconDownload />
              {engineDownloading
                ? `正在下载 ${engineDownload?.percent ?? 0}%`
                : startingEngine
                  ? '正在启动…'
                  : engineDownload?.status === 'error'
                    ? '重试下载翻译引擎'
                    : '下载翻译引擎'}
            </button>
          </div>
        </div>
      )}

      {engine ? (
        <>
          <div className="toolchain-ready">
            <IconCheck /> LaTeX 引擎已就绪{status?.ready ? '，PDF 导出可用' : ''}，无需重启。
          </div>
          {texDownload?.warning && <p className="hint">{texDownload.warning}</p>}
          <p className="hint">
            想重装或升级引擎？
            {DOWNLOADS.map((item) => (
              <a key={item.url} href={item.url} target="_blank" rel="noreferrer">
                {' '}
                {item.label.split('（')[0]} ↗
              </a>
            ))}
          </p>
        </>
      ) : (
        <div className="toolchain-install">
          <p>
            {texDownloading
              ? `正在安装 LaTeX 引擎：${texPhaseText}（${texDownload?.percent ?? 0}%），完成后自动可用。`
              : texDownload?.status === 'error'
                ? `LaTeX 引擎安装失败：${texDownload.error || '网络不可用'}`
                : '导出 PDF 需要本机 LaTeX 引擎，点下面按钮自动安装，全程无需管理员权限。'}
          </p>
          {texDownloading && (
            <div className="storage-progress">
              <div className="storage-progress-track">
                <span
                  className="storage-progress-fill"
                  style={{ width: `${Math.max(2, texDownload?.percent ?? 0)}%` }}
                />
              </div>
              <div className="storage-progress-meta">
                <span>{texPhaseText}</span>
                <span>{texDownload?.percent ?? 0}%</span>
              </div>
            </div>
          )}
          <div className="toolchain-actions">
            <button
              className="btn primary"
              onClick={() => void startTex()}
              disabled={texDownloading || startingTex}
            >
              <IconDownload />
              {texDownloading
                ? `正在安装 ${texDownload?.percent ?? 0}%`
                : startingTex
                  ? '正在启动…'
                  : texDownload?.status === 'error'
                    ? '重试安装 LaTeX 引擎'
                    : '自动安装 LaTeX 引擎'}
            </button>
            {status?.installer.winget ? (
              <button
                className="btn small ghost"
                onClick={() => void startInstall()}
                disabled={installing || texDownloading}
              >
                {installing ? '正在用 winget 安装…' : '用 winget 安装（备用）'}
              </button>
            ) : null}
            {DOWNLOADS.map((item) => (
              <a
                key={item.url}
                className="btn small ghost"
                href={item.url}
                target="_blank"
                rel="noreferrer"
              >
                {item.label} ↗
              </a>
            ))}
          </div>
          <div className="toolchain-command">
            <span>也可以在 PowerShell 里执行：</span>
            <code>{WINGET_COMMAND}</code>
            <button className="link-btn" onClick={() => void copyCommand()}>
              {copied ? '已复制' : '复制'}
            </button>
          </div>
          {job?.status === 'error' && job.error && (
            <p className="hint">winget 安装失败：{job.error}</p>
          )}
          {job && job.log.length > 0 && (
            <pre className="toolchain-log">{job.log.slice(-8).join('\n')}</pre>
          )}
          <p className="hint">
            自动安装使用 MiKTeX 便携版，装在本机数据目录、不修改系统环境，装好即可用，
            无需重启；缺宏包（含中文 ctex）会在导出时自动下载。winget 需要管理员权限，
            被拒绝时请用自动安装。
          </p>
        </div>
      )}
    </div>
  )
}
