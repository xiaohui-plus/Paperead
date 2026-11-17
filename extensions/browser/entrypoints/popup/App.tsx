import { useCallback, useEffect, useState } from 'react'
import { browser } from 'wxt/browser'
import { isPaired, listFolders, pair } from '../../lib/api'
import { ensureHostPermission } from '../../lib/pdf'
import type { PaperMetadata, TaskState } from '../../lib/types'

interface Folder {
  id: string
  name: string
  count: number
}

export function App() {
  const [paired, setPaired] = useState(false)
  const [code, setCode] = useState('')
  const [meta, setMeta] = useState<PaperMetadata | null>(null)
  const [folders, setFolders] = useState<Folder[]>([])
  const [folderId, setFolderId] = useState('')
  const [task, setTask] = useState<TaskState | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    setError('')
    const pairedNow = await isPaired()
    setPaired(pairedNow)
    if (!pairedNow) return

    try {
      setFolders(await listFolders())
    } catch {
      // 文件夹加载失败不影响保存
    }

    const [tab] = await browser.tabs.query({ active: true, currentWindow: true })
    if (!tab?.id) return
    const response = (await browser.runtime.sendMessage({
      type: 'preview',
      tabId: tab.id,
    })) as { meta?: PaperMetadata; error?: string }
    if (response.error) {
      setError(response.error)
      return
    }
    if (response.meta) setMeta(response.meta)
  }, [])

  useEffect(() => {
    void load()
    const listener = (
      changes: Record<string, { newValue?: unknown }>,
      area: string,
    ) => {
      if (area !== 'local' || !changes.tasks) return
      const tasks = (changes.tasks.newValue || {}) as Record<string, TaskState>
      const latest = Object.values(tasks).sort((a, b) =>
        a.id < b.id ? 1 : -1,
      )[0]
      setTask(latest || null)
    }
    browser.storage.onChanged.addListener(listener)
    return () => browser.storage.onChanged.removeListener(listener)
  }, [load])

  // 未配对时自动认领桌面端的等待窗口：用户只要点开弹窗就完成连接
  useEffect(() => {
    let cancelled = false
    const claim = async () => {
      if (await isPaired()) return
      try {
        await pair('')
        if (!cancelled) await load()
      } catch {
        // 没有等待窗口时留在未配对界面，「自动连接」可重试
      }
    }
    void claim()
    return () => {
      cancelled = true
    }
  }, [load])

  const connect = async () => {
    setBusy(true)
    try {
      await pair(code)
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : '配对失败')
    } finally {
      setBusy(false)
    }
  }

  const autoConnect = async () => {
    setBusy(true)
    setError('')
    try {
      await pair('')
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : '自动连接失败')
    } finally {
      setBusy(false)
    }
  }

  const save = async () => {
    const [tab] = await browser.tabs.query({ active: true, currentWindow: true })
    if (!tab?.id) return
    setBusy(true)
    setError('')
    try {
      // permissions.request 必须在用户手势里调用，放在弹窗比后台可靠
      if (meta?.pdf_url) await ensureHostPermission(meta.pdf_url)
      const response = (await browser.runtime.sendMessage({
        type: 'save',
        tabId: tab.id,
        folderId,
        pdfUrl: meta?.pdf_url && meta.pdf_url !== tab.url ? meta.pdf_url : '',
      })) as { task?: TaskState; error?: string }
      if (response.error) {
        setError(response.error)
        return
      }
      if (response.task) setTask(response.task)
    } catch (err) {
      setError(err instanceof Error ? err.message : '保存失败')
    } finally {
      setBusy(false)
    }
  }

  if (!paired) {
    return (
      <main className="popup">
        <header>
          <h1>Paperead Connector</h1>
          <p>
            在 Paperead 设置 →「插件与连接」点「连接浏览器插件」，
            然后回到这里点下面的按钮即可自动连接。
          </p>
        </header>
        <button onClick={() => void autoConnect()} disabled={busy}>
          {busy ? '连接中…' : '自动连接'}
        </button>
        <details className="manual">
          <summary>手动输入配对码</summary>
          <input
            value={code}
            onChange={(event) => setCode(event.target.value.replace(/\D/g, '').slice(0, 6))}
            placeholder="6 位配对码"
            inputMode="numeric"
          />
          <button onClick={() => void connect()} disabled={busy || code.length !== 6}>
            {busy ? '连接中…' : '连接'}
          </button>
        </details>
        {error && <p className="error">{error}</p>}
      </main>
    )
  }

  return (
    <main className="popup">
      <header>
        <h1>Paperead Connector</h1>
        <p>{meta?.title || '正在读取当前页面…'}</p>
        {meta?.authors && <p className="authors">{meta.authors}</p>}
      </header>

      {meta && (
        <dl className="meta">
          <div>
            <dt>年份</dt>
            <dd>{meta.year || '—'}</dd>
          </div>
          <div>
            <dt>期刊</dt>
            <dd>{meta.venue || '—'}</dd>
          </div>
          <div>
            <dt>DOI</dt>
            <dd>{meta.doi || '—'}</dd>
          </div>
        </dl>
      )}

      <label className="folder">
        保存到
        <select value={folderId} onChange={(event) => setFolderId(event.target.value)}>
          <option value="">全部文献</option>
          {folders.map((folder) => (
            <option key={folder.id} value={folder.id}>
              {folder.name}（{folder.count}）
            </option>
          ))}
        </select>
      </label>

      <button onClick={() => void save()} disabled={busy || !meta}>
        {busy ? '保存中…' : '保存到 Paperead'}
      </button>

      {task && (
        <p className={`task ${task.status}`}>
          {task.message}
          {task.openUrl && (
            <button className="link" onClick={() => void browser.tabs.create({ url: task.openUrl! })}>
              打开阅读页
            </button>
          )}
        </p>
      )}
      {error && <p className="error">{error}</p>}
    </main>
  )
}
