/**
 * 设置 → 设备同步。
 *
 * 电脑端：展示二维码，手机扫一下即可配对（地址、口令、模型配置）。
 * 手机端：扫码 / 从相册识别 / 粘贴同步码，写入本机后重新连接。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import QRCode from 'qrcode'
import jsQR from 'jsqr'
import {
  Format,
  cancel,
  checkPermissions,
  openAppSettings,
  requestPermissions,
  scan,
} from '@tauri-apps/plugin-barcode-scanner'
import {
  buildSyncCode,
  exportSyncPackage,
  fetchRemotePapers,
  fetchScholarReadInfo,
  fetchServerModelSettings,
  fetchSyncInfo,
  importScholarReadLibrary,
  importSyncPackage,
  parseSyncCode,
  saveSyncPackageFile,
  syncFromServer,
  type RemotePaper,
  type ScholarReadInfo,
  type SyncExportReport,
  type SyncImportProgress,
  type SyncImportReport,
  type SyncInfo,
  type SyncMode,
  type SyncReport,
} from '../lib/deviceSync'
import {
  installMobileFetch,
  loadMobileServer,
  mediaUrl,
  saveMobileServer,
  testMobileServer,
} from '../lib/mobile'
import { formatBytes, listOffline, type OfflineSummary } from '../lib/offlineStore'
import {
  loadExportHistory,
  markExported,
  type ExportHistory,
} from '../lib/syncHistory'

function CopyButton({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <button
      className="mini-btn"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(value)
          setCopied(true)
          window.setTimeout(() => setCopied(false), 1500)
        } catch {
          // 剪贴板不可用时保持原样，旁边的文本可直接选中
        }
      }}
    >
      {copied ? '已复制' : label}
    </button>
  )
}

function lanCommand(port: number) {
  return `set PAPEREAD_HOST=0.0.0.0\nset PAPEREAD_PORT=${port || 8010}\npython -m server.main`
}

type SaveOutcome = 'saved' | 'cancelled' | 'downloaded' | 'path'

/** 电脑端：勾选论文，导出一个不需要网络的 .paperead 同步包。 */
export function PackageExportPanel({
  pendingFirst = false,
}: {
  pendingFirst?: boolean
} = {}) {
  const [papers, setPapers] = useState<RemotePaper[]>([])
  const [history, setHistory] = useState<ExportHistory>(loadExportHistory)
  const [checked, setChecked] = useState<Set<string>>(new Set())
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [hint, setHint] = useState('')
  const [result, setResult] = useState<{
    report: SyncExportReport
    saved: SaveOutcome
  } | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const list = await fetchRemotePapers()
      const exported = loadExportHistory()
      setPapers(list)
      setHistory(exported)
      setChecked(
        new Set(
          list
            .filter((item) => item.status === 'ready')
            .filter((item) => !pendingFirst || !exported[item.id])
            .map((item) => item.id),
        ),
      )
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : '读取文献列表失败')
    } finally {
      setLoading(false)
    }
  }, [pendingFirst])

  useEffect(() => {
    void load()
  }, [load])

  const ready = papers.filter((item) => item.status === 'ready')
  const pending = ready.filter((item) => !history[item.id])

  const toggle = (paperId: string) => {
    setChecked((prev) => {
      const next = new Set(prev)
      if (next.has(paperId)) next.delete(paperId)
      else next.add(paperId)
      return next
    })
  }

  const runExport = async () => {
    if (busy || checked.size === 0) return
    setBusy(true)
    setError('')
    setResult(null)
    setHint('正在打包论文、页图与正文图片，篇幅多时会慢一些…')
    try {
      const report = await exportSyncPackage([...checked])
      let saved: SaveOutcome = 'path'
      try {
        const outcome = await saveSyncPackageFile(report)
        saved = outcome
      } catch (saveError) {
        setHint(
          `同步包已生成，但保存对话框没有完成：${
            saveError instanceof Error ? saveError.message : '未知错误'
          }。可直接复制下面的路径。`,
        )
      }
      setResult({ report, saved })
      markExported([...checked])
      setHistory(loadExportHistory())
      if (saved === 'cancelled') {
        setHint('已取消保存；同步包仍保留在电脑上。')
      } else if (saved === 'saved') {
        setHint('同步包已保存。把它发到手机后，在手机「设备同步」里导入。')
      } else if (saved === 'downloaded') {
        setHint('已触发浏览器下载；如果没有出现，可复制下面的电脑路径。')
      }
    } catch (exportError) {
      setHint('')
      setError(exportError instanceof Error ? exportError.message : '导出失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="model-section sync-package-export">
      <div className="section-head">
        <h3>文件同步（无需网络）</h3>
        <button className="mini-btn" onClick={() => void load()} disabled={loading || busy}>
          刷新
        </button>
      </div>
      <p className="hint">
        {pendingFirst
          ? '默认只勾选还没发送过的新论文。导出一个 .paperead 文件，发到手机后在文献库点「导入同步包」。'
          : '勾选论文后导出成一个 .paperead 文件，再用数据线、微信文件传输助手或网盘发到手机；手机在文献库点「导入同步包」。全程不需要 Wi-Fi、端口或服务器。'}
        篇数多、页图多时包会比较大，建议一次导出 5–10 篇。
      </p>

      {loading ? (
        <p className="hint">正在读取文献库…</p>
      ) : papers.length === 0 ? (
        <p className="hint">文献库还没有论文。先导入并等待解析完成，再来导出。</p>
      ) : (
        <>
          <div className="sync-package-toolbar">
            <span>
              已就绪 <b>{ready.length}</b> 篇 · 已发送 <b>{ready.length - pending.length}</b> 篇
            </span>
            <button
              className="mini-btn"
              onClick={() => setChecked(new Set(pending.map((item) => item.id)))}
              disabled={busy || pending.length === 0}
            >
              仅选未发送
            </button>
            <button
              className="mini-btn"
              onClick={() => setChecked(new Set(ready.map((item) => item.id)))}
              disabled={busy}
            >
              全选
            </button>
            <button
              className="mini-btn"
              onClick={() => setChecked(new Set())}
              disabled={busy}
            >
              清空
            </button>
          </div>
          <div className="sync-card-grid">
            {papers.map((paper) => {
              const isReady = paper.status === 'ready'
              const initial = (paper.title || '?').trim().slice(0, 1).toUpperCase()
              return (
                <label
                  key={paper.id}
                  className="sync-card"
                  data-checked={checked.has(paper.id) || undefined}
                  data-disabled={!isReady || undefined}
                >
                  <input
                    type="checkbox"
                    checked={checked.has(paper.id)}
                    disabled={!isReady || busy}
                    onChange={() => toggle(paper.id)}
                  />
                  <div className="sync-card-cover" data-tone="phone">
                    {paper.thumbUrl ? (
                      <img
                        src={mediaUrl(paper.thumbUrl)}
                        alt=""
                        loading="lazy"
                      />
                    ) : (
                      <span>{initial}</span>
                    )}
                    <em>
                      {!isReady ? '解析中' : history[paper.id] ? '已发送' : ''}
                    </em>
                  </div>
                  <div className="sync-card-info">
                    <b title={paper.title}>{paper.title}</b>
                    <span>
                      {paper.authors || '作者未知'} · {paper.pages} 页 ·{' '}
                      {formatBytes(paper.size)}
                    </span>
                  </div>
                </label>
              )
            })}
          </div>
          {pendingFirst && pending.length === 0 && (
            <p className="hint">
              所有已就绪论文都发送过了。需要重新发送时，点上面的「全选」再导出。
            </p>
          )}
          <button
            className="btn primary"
            onClick={() => void runExport()}
            disabled={busy || checked.size === 0}
          >
            {busy
              ? '正在打包…'
              : pendingFirst
                ? `发送 ${checked.size} 篇新论文`
                : `导出同步包（${checked.size} 篇）`}
          </button>
        </>
      )}

      {hint && <p className="hint">{hint}</p>}
      {error && <p className="settings-error">{error}</p>}

      {result && (
        <div className="sync-report">
          <b>同步包已生成</b>
          <p>
            {result.report.count} 篇 · {formatBytes(result.report.size)}
          </p>
          <p className="sync-package-path">
            在电脑上：<code>{result.report.path}</code>
            <CopyButton value={result.report.path} label="复制路径" />
          </p>
          {result.saved === 'saved' && <p>已保存到你选择的位置。</p>}
          {result.saved === 'downloaded' && (
            <p>已触发浏览器下载；若没有出现，可直接使用上面的路径。</p>
          )}
          {result.report.skipped.length > 0 && (
            <ul>
              {result.report.skipped.slice(0, 5).map((item) => (
                <li key={item.id}>
                  {item.title}：{item.reason}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      <p className="hint">
        传输方式：数据线拷贝、微信「文件传输助手」、QQ、网盘、隔空投送都可以，文件不会被上传到 Paperead 的服务器。
      </p>
    </section>
  )
}

/** 电脑端：二维码 + 连接信息。 */
function WifiPairPanel() {
  const [info, setInfo] = useState<SyncInfo | null>(null)
  const [qr, setQr] = useState('')
  const [error, setError] = useState('')
  const [note, setNote] = useState('')
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    setNote('')
    try {
      const next = await fetchSyncInfo()
      setInfo(next)
      if (next.exposed) {
        const settings = await fetchServerModelSettings()
        const options = { width: 260, margin: 1, errorCorrectionLevel: 'L' } as const
        try {
          setQr(await QRCode.toDataURL(buildSyncCode(next, settings), options))
        } catch {
          // 模型配置太大会超出二维码容量：退回只带地址和口令，
          // 手机连上后启动时会自动从服务器拉取模型配置。
          setQr(await QRCode.toDataURL(buildSyncCode(next, null), options))
          setNote('模型配置较大，二维码只包含地址和口令；手机连上后会自动拉取模型配置。')
        }
      } else {
        setQr('')
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : '读取设备信息失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  if (loading) return <div className="sync-wifi"><p className="hint">正在读取本机连接信息…</p></div>

  return (
    <div className="sync-wifi">
      <section className="model-section">
        <div className="section-head">
          <h3>手机配对</h3>
          <button className="mini-btn" onClick={() => void load()}>
            重新检测
          </button>
        </div>

        {error && <p className="settings-error">{error}</p>}

        {info?.exposed && qr ? (
          <>
            <div className="sync-qr-card">
              <img className="sync-qr" src={qr} alt="手机配对二维码" />
              <div className="sync-meta">
                <p>
                  用手机打开 <b>设置 → 设备同步 → 扫描二维码</b>，自动填入服务器地址、访问口令和模型配置。
                </p>
                <dl>
                  <div>
                    <dt>服务器</dt>
                    <dd>
                      <code>{info.base}</code>
                      <CopyButton value={info.base} label="复制" />
                    </dd>
                  </div>
                  <div>
                    <dt>访问口令</dt>
                    <dd>
                      <code>{info.token || '（未设置）'}</code>
                      {info.token && <CopyButton value={info.token} label="复制" />}
                    </dd>
                  </div>
                  <div>
                    <dt>电脑</dt>
                    <dd>{info.name} · v{info.version}</dd>
                  </div>
                </dl>
                <p className="sync-tip">二维码包含模型配置，请勿截图外传。手机与电脑需在同一 Wi-Fi。</p>
                {note && <p className="sync-tip">{note}</p>}
              </div>
            </div>
          </>
        ) : (
          <div className="sync-warning">
            <b>当前电脑只允许本机访问，手机还连不上</b>
            <p>
              先关掉正在运行的 Paperead 服务，然后在项目目录用下面的命令重新启动（Windows）：
            </p>
            <pre>{lanCommand(info?.port ?? 8010)}</pre>
            <p>
              启动后回到这里点「重新检测」即可看到二维码。若仍不通，请允许 Windows 防火墙中的
              Python / Paperead 入站。
            </p>
          </div>
        )}
      </section>
    </div>
  )
}

/** 设置 → 设备同步（电脑端）：文件同步为主，Wi-Fi 直连收进高级。 */
export function DesktopDeviceSyncPanel() {
  return (
    <div className="modal-body">
      <PackageExportPanel />
      <details className="sync-advanced">
        <summary>Wi-Fi 直连（高级，可选）</summary>
        <WifiPairPanel />
      </details>
    </div>
  )
}

/** 从相册图片里识别二维码。 */
async function decodeQrImage(file: File): Promise<string | null> {
  const url = URL.createObjectURL(file)
  try {
    const image = await new Promise<HTMLImageElement>((resolve, reject) => {
      const element = new Image()
      element.onload = () => resolve(element)
      element.onerror = () => reject(new Error('图片读取失败'))
      element.src = url
    })
    const scale = Math.min(1, 900 / Math.max(image.width, image.height))
    const canvas = document.createElement('canvas')
    canvas.width = Math.max(1, Math.round(image.width * scale))
    canvas.height = Math.max(1, Math.round(image.height * scale))
    const context = canvas.getContext('2d')
    if (!context) return null
    context.drawImage(image, 0, 0, canvas.width, canvas.height)
    const data = context.getImageData(0, 0, canvas.width, canvas.height)
    return jsQR(data.data, data.width, data.height)?.data ?? null
  } finally {
    URL.revokeObjectURL(url)
  }
}

type SyncView = 'main' | 'select'

/** 手机端：从系统文件选择器导入电脑导出的 .paperead，不依赖网络。 */
function PackageImportPanel({
  onImported,
  onDone,
}: {
  onImported?: () => void
  onDone?: () => void
}) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [progress, setProgress] = useState<SyncImportProgress | null>(null)
  const [report, setReport] = useState<SyncImportReport | null>(null)

  const choose = () => {
    setError('')
    setReport(null)
    inputRef.current?.click()
  }

  const run = async (file: File | null) => {
    if (!file || busy) return
    setBusy(true)
    setError('')
    setReport(null)
    setProgress({ done: 0, total: 0, title: '' })
    try {
      const result = await importSyncPackage(file, setProgress)
      setReport(result)
      onImported?.()
    } catch (importError) {
      setError(
        importError instanceof Error ? importError.message : '导入同步包失败',
      )
    } finally {
      setBusy(false)
      setProgress(null)
    }
  }

  return (
    <section className="model-section sync-package-import">
      <div className="section-head">
        <h3>从电脑导入同步包</h3>
      </div>
      <p className="hint">
        电脑上打开「设置 → 设备同步 → 文件同步」，导出 <code>.paperead</code> 文件，
        再通过微信、数据线或网盘发到手机。不需要和电脑连同一个 Wi-Fi；同一篇论文会覆盖手机上的旧副本。
      </p>
      <input
        ref={inputRef}
        type="file"
        accept=".paperead,.zip,application/zip"
        hidden
        onChange={(event) => {
          const file = event.target.files?.[0] ?? null
          event.target.value = ''
          void run(file)
        }}
      />
      <button className="btn primary" onClick={choose} disabled={busy}>
        {busy ? '正在导入…' : '选择 .paperead 文件'}
      </button>

      {busy && progress && (
        <div className="sync-progress">
          <div className="sync-progress-bar">
            <span
              style={{
                width: `${
                  progress.total
                    ? Math.round((progress.done / progress.total) * 100)
                    : 8
                }%`,
              }}
            />
          </div>
          <span className="sync-progress-text">
            {progress.total
              ? `已导入 ${Math.min(progress.done, progress.total)}/${progress.total} · ${
                  progress.title || '…'
                }`
              : '正在读取同步包…'}
          </span>
        </div>
      )}

      {error && <p className="settings-error">{error}</p>}

      {report && (
        <div className="sync-report">
          <b>导入完成</b>
          <p>
            共 {report.total} 篇：新增 {report.imported} 篇
            {report.updated ? `，覆盖 ${report.updated} 篇` : ''}。
          </p>
          {report.failed.length > 0 && (
            <ul>
              {report.failed.slice(0, 5).map((item) => (
                <li key={item.title}>
                  {item.title}：{item.reason}
                </li>
              ))}
              {report.failed.length > 5 && (
                <li>还有 {report.failed.length - 5} 篇失败…</li>
              )}
            </ul>
          )}
          <p className="hint">回到文献库即可离线阅读。</p>
          {onDone && (
            <button className="btn primary sync-done-btn" onClick={onDone}>
              进入离线文献库
            </button>
          )}
        </div>
      )}
    </section>
  )
}

/** 手机端：配对后直接选同步方式；没配对时先扫码 / 相册 / 粘贴同步码。 */
export function MobileDeviceSyncPanel({
  onDone,
  bare = false,
}: { onDone?: () => void; bare?: boolean } = {}) {
  const [server, setServer] = useState(loadMobileServer)
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [pairOpen, setPairOpen] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)
  const native = typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window

  const [remote, setRemote] = useState<RemotePaper[]>([])
  const [local, setLocal] = useState<OfflineSummary[]>([])
  const [scholar, setScholar] = useState<ScholarReadInfo | null>(null)
  const [importing, setImporting] = useState(false)
  const [loadingRemote, setLoadingRemote] = useState(false)
  const [remoteError, setRemoteError] = useState('')
  const [view, setView] = useState<SyncView>('main')
  const [checked, setChecked] = useState<Set<string>>(new Set())
  const [running, setRunning] = useState(false)
  const [progress, setProgress] = useState<{
    done: number
    total: number
    title: string
  } | null>(null)
  const [report, setReport] = useState<SyncReport | null>(null)
  const [confirmFull, setConfirmFull] = useState(false)
  const [scanning, setScanning] = useState(false)
  const cancelledByUser = useRef(false)

  const paired = Boolean(server.base)
  const localIds = new Set(local.map((item) => item.id))
  const readyCount = remote.filter((item) => item.status === 'ready').length

  const loadRemote = useCallback(async () => {
    if (!loadMobileServer().base) return
    setLoadingRemote(true)
    setRemoteError('')
    try {
      const [papers, items] = await Promise.all([fetchRemotePapers(), listOffline()])
      setRemote(papers)
      setLocal(items)
    } catch (e) {
      setRemoteError(e instanceof Error ? e.message : '读取电脑端文献失败')
      try {
        setLocal(await listOffline())
      } catch {
        // 本机离线库也读不到时保持原样
      }
    } finally {
      // 学术阅读是可选能力：检测失败不影响正常同步
      setScholar(await fetchScholarReadInfo().catch(() => null))
      setLoadingRemote(false)
    }
  }, [])

  useEffect(() => {
    void loadRemote()
  }, [loadRemote])

  const refreshLocal = useCallback(async () => {
    try {
      setLocal(await listOffline())
    } catch {
      // 读不到本机列表时保持原样，不影响导入结果展示
    }
  }, [])

  // 扫码时把网页变成透明取景层，让插件叠加在 WebView 下面的相机画面透出来。
  useEffect(() => {
    if (!scanning) return
    const root = document.documentElement
    root.dataset.scanning = '1'
    return () => {
      delete root.dataset.scanning
    }
  }, [scanning])

  const apply = useCallback(
    async (raw: string) => {
      const parsed = parseSyncCode(raw)
      if (!parsed) {
        setError('没有识别出 Paperead 同步码，请重新扫描电脑上的二维码')
        return
      }
      setBusy(true)
      setError('')
      setNotice('')
      try {
        let reachable = true
        try {
          await testMobileServer(parsed.base, parsed.token)
        } catch {
          // 暂时连不上也照样保存：用户可能晚点回到同一 Wi-Fi 再同步。
          // 这里不用 window.confirm，安卓 WebView 不支持原生确认框。
          reachable = false
        }
        saveMobileServer(parsed.base, parsed.token)
        // 配对后立即让本页的 /api 请求走新地址，不必重启 App
        installMobileFetch()
        if (parsed.settings) {
          localStorage.setItem('paperead.models', JSON.stringify(parsed.settings))
        }
        setServer(loadMobileServer())
        setPairOpen(false)
        setView('main')
        setReport(null)
        setNotice(reachable ? '配对成功，选一种方式把电脑上的文献拉到手机' : '已保存配对信息（当前连不上电脑）')
        if (reachable) void loadRemote()
      } catch (e) {
        setError(e instanceof Error ? e.message : '保存配对信息失败')
      } finally {
        setBusy(false)
      }
    },
    [loadRemote],
  )

  const startScan = useCallback(async () => {
    setError('')
    setNotice('')
    cancelledByUser.current = false
    try {
      let state = await checkPermissions().catch(() => null)
      if (state !== 'granted') {
        state = await requestPermissions()
      }
      if (state !== 'granted') {
        throw new Error('没有相机权限，请在系统设置里允许 Paperead 使用相机')
      }
      // 权限拿到后再切透明取景层，避免权限弹窗期间页面被隐藏。
      setScanning(true)
      const result = await scan({ windowed: true, formats: [Format.QRCode] })
      await apply(result.content)
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e)
      if (/cancel/i.test(message)) {
        // 用户自己点了取消就安静收起；插件/系统提前结束扫描时给个可见提示。
        if (!cancelledByUser.current) {
          setNotice('扫描已取消，可以再试一次，或点「从相册识别」')
        }
        return
      }
      setError(`${message}；也可以点「从相册识别」或粘贴同步码`)
    } finally {
      setScanning(false)
    }
  }, [apply])

  const runSync = useCallback(
    async (mode: SyncMode, ids?: string[]) => {
      setRunning(true)
      setReport(null)
      setError('')
      setNotice('')
      setProgress({ done: 0, total: 0, title: '' })
      try {
        const result = await syncFromServer({ mode, ids, onProgress: setProgress })
        setReport(result)
        setConfirmFull(false)
        setView('main')
        setChecked(new Set())
        await loadRemote()
      } catch (e) {
        setError(e instanceof Error ? e.message : '同步失败')
      } finally {
        setProgress(null)
        setRunning(false)
      }
    },
    [loadRemote],
  )

  /** 让电脑先把 Scholaread 里已下载的文献导入文库，再合并到手机。 */
  const importAndSync = useCallback(async () => {
    setImporting(true)
    setRunning(true)
    setReport(null)
    setError('')
    setNotice('')
    setProgress({ done: 0, total: 0, title: '' })
    try {
      const imported = await importScholarReadLibrary()
      const result = await syncFromServer({ mode: 'merge', onProgress: setProgress })
      setReport({
        ...result,
        scholaread: {
          imported: imported.imported,
          linked: imported.linked,
          skippedNoPdf: imported.skippedNoPdf,
        },
      })
      if (imported.errors.length > 0) {
        setError(imported.errors.slice(0, 3).join('；'))
      }
      if (imported.running) {
        setNotice('学术阅读正在电脑上运行，本次只导入，不改动它的数据')
      }
      setView('main')
      setChecked(new Set())
      await loadRemote()
    } catch (e) {
      setError(e instanceof Error ? e.message : '从电脑端学术阅读导入失败')
    } finally {
      setProgress(null)
      setRunning(false)
      setImporting(false)
    }
  }, [loadRemote])

  const toggle = (paperId: string) => {
    setChecked((prev) => {
      const next = new Set(prev)
      if (next.has(paperId)) next.delete(paperId)
      else next.add(paperId)
      return next
    })
  }

  const selectAllMissing = () => {
    setChecked(
      new Set(
        remote
          .filter((paper) => paper.status === 'ready' && !localIds.has(paper.id))
          .map((paper) => paper.id),
      ),
    )
  }

  return (
    <div className="modal-body">
      {scanning && (
        <div
          className="scan-overlay"
          role="dialog"
          aria-modal="true"
          aria-label="扫描二维码"
        >
          <div className="scan-frame" />
          <p>把电脑「设备同步」里的二维码放进框内</p>
          <button
            className="btn ghost"
            onClick={() => {
              cancelledByUser.current = true
              void cancel()
            }}
          >
            取消扫描
          </button>
        </div>
      )}
      <PackageImportPanel
        onImported={() => void refreshLocal()}
        onDone={onDone}
      />
      <details className="sync-advanced">
        <summary>
          {paired ? 'Wi-Fi 直连（已连接）' : 'Wi-Fi 直连（高级，可选）'}
        </summary>
        {paired && !pairOpen ? (
        <section className="model-section">
          <div className="section-head">
            <h3>同步电脑端文献</h3>
            <button
              className="mini-btn"
              onClick={() => void loadRemote()}
              disabled={loadingRemote || running}
            >
              刷新
            </button>
          </div>
          <p className="hint">
            已连接 <code>{server.base}</code>。手机和电脑需在同一个 Wi-Fi。
          </p>
          {notice && <p className="hint">{notice}</p>}

          {loadingRemote ? (
            <p className="hint">正在读取电脑端文献…</p>
          ) : remoteError ? (
            <div className="sync-warning">
              <b>暂时读不到电脑端文献</b>
              <p>{remoteError}</p>
              <p>确认电脑端 Paperead 正在运行、手机与电脑在同一 Wi-Fi，然后点「刷新」。</p>
            </div>
          ) : view === 'main' ? (
            <>
              <div className="sync-stats">
                <span>
                  电脑端 <b>{remote.length}</b> 篇（已就绪 {readyCount}）
                </span>
                <span>
                  手机本机 <b>{local.length}</b> 篇
                </span>
              </div>

              {scholar?.ok && scholar.total > 0 && (
                <div className="sync-scholaread">
                  <div className="sync-scholaread-meta">
                    <b>学术阅读（Scholaread）</b>
                    <span>
                      电脑上已下载 {scholar.total} 篇，其中 {scholar.withPdf} 篇可导入
                      {scholar.running ? ' · 它正在运行，只导入不改动' : ''}
                    </span>
                  </div>
                  <button
                    className="mini-btn"
                    onClick={() => void importAndSync()}
                    disabled={running || importing || scholar.withPdf === 0}
                  >
                    {importing ? '导入中…' : '导入并拉取'}
                  </button>
                </div>
              )}

              <div className="sync-mode-grid">
                <button
                  className="sync-mode-card is-primary"
                  onClick={() => void runSync('merge')}
                  disabled={running}
                >
                  <b>并集合并</b>
                  <span>保留手机已有内容，只补电脑端新增的论文</span>
                </button>
                <button
                  className="sync-mode-card"
                  onClick={() => {
                    setView('select')
                    setReport(null)
                  }}
                  disabled={running}
                >
                  <b>选择拉取</b>
                  <span>自己挑选要拉到手机的论文</span>
                </button>
                <button
                  className="sync-mode-card is-danger"
                  onClick={() => setConfirmFull(true)}
                  disabled={running}
                >
                  <b>全部拉取</b>
                  <span>电脑端全部文献和文件都拉到手机，覆盖本机副本</span>
                </button>
              </div>

              {confirmFull && (
                <div className="sync-confirm">
                  <b>全部拉取会覆盖手机上的论文副本</b>
                  <p>
                    电脑端 {remote.length} 篇都会重新下载到本机；手机上有、电脑上没有的论文副本会被删除
                    （从 StoryReader 导入的书保留）。
                  </p>
                  <div className="sync-scan-actions">
                    <button
                      className="btn primary"
                      onClick={() => void runSync('full')}
                      disabled={running}
                    >
                      确认全部拉取
                    </button>
                    <button
                      className="btn ghost"
                      onClick={() => setConfirmFull(false)}
                      disabled={running}
                    >
                      取消
                    </button>
                  </div>
                </div>
              )}
            </>
          ) : (
            <div className="sync-select">
              <div className="section-head">
                <h3>选择要拉取的论文</h3>
                <button
                  className="mini-btn"
                  onClick={() => setView('main')}
                  disabled={running}
                >
                  返回
                </button>
              </div>
              <div className="sync-scan-actions">
                <button className="mini-btn" onClick={selectAllMissing} disabled={running}>
                  全选未下载
                </button>
                <button
                  className="mini-btn"
                  onClick={() => setChecked(new Set())}
                  disabled={running}
                >
                  清空
                </button>
              </div>
              {remote.length === 0 ? (
                <p className="hint">电脑端还没有文献。</p>
              ) : (
                <div className="sync-card-grid">
                  {remote.map((paper) => {
                    const saved = localIds.has(paper.id)
                    const ready = paper.status === 'ready'
                    const initial = (paper.title || '?').trim().slice(0, 1).toUpperCase()
                    return (
                      <label
                        key={paper.id}
                        className="sync-card"
                        data-checked={checked.has(paper.id) || undefined}
                        data-disabled={saved || !ready || undefined}
                      >
                        <input
                          type="checkbox"
                          checked={checked.has(paper.id)}
                          disabled={saved || !ready || running}
                          onChange={() => toggle(paper.id)}
                        />
                        <div className="sync-card-cover" data-tone="phone">
                          {paper.thumbUrl ? (
                            <img
                              src={mediaUrl(paper.thumbUrl)}
                              alt=""
                              loading="lazy"
                            />
                          ) : (
                            <span>{initial}</span>
                          )}
                          <em>{saved ? '已在本机' : ready ? '' : '解析中'}</em>
                        </div>
                        <div className="sync-card-info">
                          <b title={paper.title}>{paper.title}</b>
                          <span>
                            {paper.authors || '作者未知'} · {paper.pages} 页 ·{' '}
                            {formatBytes(paper.size)}
                          </span>
                        </div>
                      </label>
                    )
                  })}
                </div>
              )}
              <button
                className="btn primary"
                onClick={() => void runSync('select', [...checked])}
                disabled={running || checked.size === 0}
              >
                拉取选中的 {checked.size} 篇
              </button>
            </div>
          )}

          {running && (
            <div className="sync-progress">
              <div className="sync-progress-bar">
                <span
                  style={{
                    width: `${
                      progress && progress.total
                        ? Math.round((progress.done / progress.total) * 100)
                        : 0
                    }%`,
                  }}
                />
              </div>
              <span className="sync-progress-text">
                {progress && progress.total
                  ? `正在拉取 ${Math.min(progress.done + 1, progress.total)}/${progress.total} · ${
                      progress.title || '…'
                    }`
                  : '正在读取电脑端文献…'}
              </span>
            </div>
          )}

          {error && <p className="settings-error">{error}</p>}

          {report && (
            <div className="sync-report">
              <b>同步完成</b>
              <p>
                下载 {report.downloaded} 篇
                {report.skipped ? `，跳过已有 ${report.skipped} 篇` : ''}
                {report.removed ? `，删除本机多余 ${report.removed} 篇` : ''}。
              </p>
              {report.scholaread && (
                <p>
                  学术阅读导入 {report.scholaread.imported} 篇
                  {report.scholaread.linked ? `，关联已有 ${report.scholaread.linked} 篇` : ''}
                  {report.scholaread.skippedNoPdf
                    ? `，${report.scholaread.skippedNoPdf} 篇缺少本机 PDF`
                    : ''}
                  。新导入的文献电脑端还要解析，解析完成的会在下次「并集合并」时拉到手机。
                </p>
              )}
              {report.failed.length > 0 && (
                <ul>
                  {report.failed.slice(0, 5).map((item) => (
                    <li key={item.id}>
                      {item.title}：{item.reason}
                    </li>
                  ))}
                  {report.failed.length > 5 && (
                    <li>还有 {report.failed.length - 5} 篇失败…</li>
                  )}
                </ul>
              )}
              <p className="hint">回到文献库即可离线阅读。</p>
            </div>
          )}

          <div className="sync-footer">
            <button
              className="mini-btn"
              onClick={() => {
                setPairOpen(true)
                setError('')
                setNotice('')
              }}
            >
              重新扫码配对
            </button>
            <span>{server.token ? '已设置访问口令' : '未设置访问口令'}</span>
          </div>
          {onDone && (
            <button className="btn primary sync-done-btn" onClick={onDone}>
              进入文献库
            </button>
          )}
        </section>
      ) : (
        <section className={bare ? 'model-section is-bare' : 'model-section'}>
          {!bare && (
            <>
              <div className="section-head">
                <h3>{paired ? '重新配对' : '扫描电脑上的二维码'}</h3>
                {paired && (
                  <button className="mini-btn" onClick={() => setPairOpen(false)} disabled={busy}>
                    返回
                  </button>
                )}
              </div>
              <p className="hint">
                在电脑上打开 <b>设置 → 设备同步</b>，用下面的按钮扫描二维码。地址、访问口令和模型配置会一起同步到本机。
              </p>
            </>
          )}

          <div className="sync-scan-actions">
            {native && (
              <button
                className="btn primary"
                onClick={() => void startScan()}
                disabled={busy || scanning}
              >
                扫描二维码
              </button>
            )}
            {native && (
              <button
                className="mini-btn"
                onClick={() => void openAppSettings()}
              >
                相机权限
              </button>
            )}
            <button
              className="btn ghost"
              onClick={() => fileRef.current?.click()}
              disabled={busy}
            >
              从相册识别
            </button>
            <input
              ref={fileRef}
              type="file"
              accept="image/*"
              hidden
              onChange={async (event) => {
                const file = event.target.files?.[0]
                event.target.value = ''
                if (!file) return
                setError('')
                try {
                  const text = await decodeQrImage(file)
                  if (!text) {
                    setError('图片里没有识别到二维码')
                    return
                  }
                  await apply(text)
                } catch (e) {
                  setError(e instanceof Error ? e.message : '识别失败')
                }
              }}
            />
          </div>

          <label className="sync-manual">
            <span>或者粘贴同步码</span>
            <textarea
              value={code}
              onChange={(event) => setCode(event.target.value)}
              placeholder='{"v":1,"base":"http://192.168.1.5:8010","token":"..."}'
              rows={3}
            />
          </label>
          <button
            className="btn ghost"
            onClick={() => void apply(code)}
            disabled={busy || !code.trim()}
          >
            使用这段同步码
          </button>

          {error && <p className="settings-error">{error}</p>}
          {notice && <p className="hint">{notice}</p>}

          <p className="hint">
            当前连接：<code>{server.base || '未配置'}</code>
            {server.token ? '（已设置口令）' : ''}
          </p>
</section>
        )}
      </details>
    </div>
  )
}
