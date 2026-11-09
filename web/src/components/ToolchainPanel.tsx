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
  engines: { name: string; path: string; version: string }[]
  bibtex: ToolInfo
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

  const load = useCallback(async (silent = false) => {
    if (!silent) setLoading(true)
    try {
      const res = await fetch('/api/toolchain')
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

  // pandoc 首次启动在后台下载，这里跟着进度刷新，下载完自动变成“已就绪”。
  const pandocState = status?.pandocDownload?.status
  useEffect(() => {
    if (pandocState !== 'running') return
    const timer = window.setInterval(() => void load(true), 2000)
    return () => window.clearInterval(timer)
  }, [pandocState, load])

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
        if (data.status !== 'running') void load()
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
        await load()
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

  const engine = status?.engines[0]
  const installing = job?.status === 'running' || starting
  const download = status?.pandocDownload
  const downloading = download?.status === 'running'

  return (
    <div className="modal-body toolchain-panel">
      <section className="toolchain-head">
        <div>
          <h3>写作工具</h3>
          <p>
            首次启动会自动下载 pandoc（约 24MB，只需一次），之后离线可用。
            <br />
            导出 PDF 还需要本机的 LaTeX 引擎，可以在下面一键安装。
          </p>
        </div>
        <button className="btn small" onClick={() => void load()} disabled={loading}>
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
          detail={engine ? `${engine.name} · ${engine.version}`.trim() : ''}
        />
        <ToolRow
          name="BibTeX"
          hint="参考文献排序与格式化"
          ok={Boolean(status?.bibtex.found)}
          detail={status?.bibtex.version || ''}
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

      {status?.ready ? (
        <>
          <div className="toolchain-ready">
            <IconCheck /> PDF 导出已可用。
          </div>
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
            {job?.status === 'done'
              ? '安装完成。如果仍显示未检测到，请重启应用让新的 PATH 生效。'
              : job?.status === 'error'
                ? job.error || '安装失败，可以改用官网安装包。'
                : '没有检测到 TeX 引擎，导出 PDF 会失败。'}
          </p>
          <div className="toolchain-actions">
            {status?.installer.winget ? (
              <button
                className="btn primary"
                onClick={() => void startInstall()}
                disabled={installing}
              >
                <IconDownload /> {installing ? '正在安装 MiKTeX…' : '一键安装 MiKTeX'}
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
          {status && !status.installer.winget && (
            <p className="hint">没有检测到 winget，请用上面的官网安装包。</p>
          )}
          <div className="toolchain-command">
            <span>也可以在 PowerShell 里执行：</span>
            <code>{WINGET_COMMAND}</code>
            <button className="link-btn" onClick={() => void copyCommand()}>
              {copied ? '已复制' : '复制'}
            </button>
          </div>
          {job && job.log.length > 0 && (
            <pre className="toolchain-log">{job.log.slice(-8).join('\n')}</pre>
          )}
          <p className="hint">
            MiKTeX 约 250MB，首次编译时会按需下载宏包；安装完成后需要重启应用才能识别。
          </p>
        </div>
      )}
    </div>
  )
}
