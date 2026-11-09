import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  importZotero,
  syncZotero,
  zoteroCollections,
  zoteroItems,
  zoteroStatus,
  type ZoteroCollection,
  type ZoteroItem,
  type ZoteroStatus,
} from '../lib/library'
import { IconClose } from './icons'

interface Props {
  open: boolean
  onClose: () => void
  /** 一批导入结束后通知父级刷新文献库 */
  onImported: (imported: number, skipped: number) => void
}

const PAGE_SIZE = 50

type Result = { imported: number; skipped: number; errors: string[] }

export function ZoteroImportDialog({ open, onClose, onImported }: Props) {
  const [status, setStatus] = useState<ZoteroStatus | null>(null)
  const [checking, setChecking] = useState(false)
  const [collections, setCollections] = useState<ZoteroCollection[]>([])
  const [collection, setCollection] = useState('')
  const [query, setQuery] = useState('')
  const [debounced, setDebounced] = useState('')
  const [items, setItems] = useState<ZoteroItem[]>([])
  const [total, setTotal] = useState(0)
  const [hasMore, setHasMore] = useState(false)
  const [loading, setLoading] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [importing, setImporting] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [progress, setProgress] = useState({ done: 0, total: 0 })
  const [result, setResult] = useState<Result | null>(null)
  const reqId = useRef(0)

  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(query.trim()), 300)
    return () => window.clearTimeout(timer)
  }, [query])

  const loadItems = useCallback(
    async (start: number, more = false) => {
      const id = ++reqId.current
      if (more) setLoadingMore(true)
      else setLoading(true)
      try {
        const page = await zoteroItems({
          collection,
          q: debounced,
          limit: PAGE_SIZE,
          start,
        })
        if (id !== reqId.current) return
        setItems((prev) => (more ? [...prev, ...page.items] : page.items))
        setTotal(page.total)
        setHasMore(page.has_more)
        if (!more) {
          const visible = new Set(page.items.map((item) => item.key))
          setSelected((prev) => new Set([...prev].filter((key) => visible.has(key))))
        }
      } catch (e) {
        if (id === reqId.current) {
          setStatus({
            ok: false,
            error: e instanceof Error ? e.message : '读取 Zotero 文献库失败',
          })
        }
      } finally {
        if (id === reqId.current) {
          setLoading(false)
          setLoadingMore(false)
        }
      }
    },
    [collection, debounced],
  )

  const connect = useCallback(async () => {
    setChecking(true)
    setStatus(null)
    setResult(null)
    setSelected(new Set())
    try {
      const next = await zoteroStatus()
      setStatus(next)
      if (next.ok) {
        setCollections(await zoteroCollections().catch(() => []))
      }
    } catch (e) {
      setStatus({
        ok: false,
        error: e instanceof Error ? e.message : '无法连接 Zotero',
      })
    } finally {
      setChecking(false)
    }
  }, [])

  // 打开时重置并检测连接；关闭时丢弃进行中的列表请求
  useEffect(() => {
    if (!open) {
      reqId.current++
      return
    }
    setQuery('')
    setDebounced('')
    setCollection('')
    setItems([])
    setTotal(0)
    void connect()
  }, [open, connect])

  // 连接成功、分类或搜索变化时重新拉第一页
  useEffect(() => {
    if (!open || !status?.ok) return
    void loadItems(0)
  }, [open, status?.ok, loadItems])

  const importable = useMemo(
    () => items.filter((item) => item.pdf && !item.imported),
    [items],
  )
  const allSelected =
    importable.length > 0 && importable.every((item) => selected.has(item.key))

  const toggle = (key: string) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  const toggleAll = () => {
    if (!importable.length) return
    setSelected((prev) => {
      const next = new Set(prev)
      if (importable.every((item) => prev.has(item.key))) {
        for (const item of importable) next.delete(item.key)
      } else {
        for (const item of importable) next.add(item.key)
      }
      return next
    })
  }

  const doImport = async () => {
    const keys = [...selected]
    if (!keys.length || importing) return
    setImporting(true)
    setResult(null)
    setProgress({ done: 0, total: keys.length })
    let imported = 0
    let skipped = 0
    const errors: string[] = []
    // 逐篇请求，让进度条能真实反映当前导入到第几篇
    for (let i = 0; i < keys.length; i++) {
      setProgress({ done: i, total: keys.length })
      try {
        const res = await importZotero([keys[i]])
        imported += res.imported
        skipped += res.skipped
        errors.push(...res.errors)
      } catch (e) {
        errors.push(e instanceof Error ? e.message : '导入失败')
      }
    }
    setProgress({ done: keys.length, total: keys.length })
    setImporting(false)
    setResult({ imported, skipped, errors })
    setSelected(new Set())
    onImported(imported, skipped)
    void loadItems(0)
  }

  const doSync = async () => {
    if (syncing || importing) return
    setSyncing(true)
    setResult(null)
    try {
      const res = await syncZotero(collection)
      setResult({
        imported: res.imported,
        skipped: res.skipped,
        errors: res.errors,
      })
      onImported(res.imported, res.skipped)
      await loadItems(0)
    } catch (e) {
      setResult({
        imported: 0,
        skipped: 0,
        errors: [e instanceof Error ? e.message : '同步失败'],
      })
    } finally {
      setSyncing(false)
    }
  }

  if (!open) return null

  const percent = progress.total ? (progress.done / progress.total) * 100 : 0

  return (
    <div className="modal-mask" onMouseDown={onClose}>
      <div
        className="modal zotero-modal"
        onMouseDown={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal
        aria-label="从 Zotero 导入"
      >
        <header className="modal-head">
          <div>
            <h2>从 Zotero 导入</h2>
            <p>读取本机 Zotero 文献库，只导入带 PDF 附件的条目，导入后自动解析。</p>
          </div>
          <button className="icon-btn" onClick={onClose} title="关闭">
            <IconClose />
          </button>
        </header>

        {checking ? (
          <div className="modal-body zotero-empty">
            <div className="spinner" />
            正在连接本机 Zotero…
          </div>
        ) : status && !status.ok ? (
          <div className="modal-body zotero-fail">
            <p>{status.error || '无法连接本机 Zotero。'}</p>
            <ol>
              <li>打开 Zotero 桌面端（需 7 及以上版本）。</li>
              <li>
                在设置窗口左侧点「高级」，在最上方的「杂项」里勾选
                “允许此计算机上的其他应用程序与 Zotero 通讯”。
                找不到时可在设置右上角的搜索框输入「通讯」。
              </li>
              <li>
                若该勾选框是灰色，先点旁边的「启用并重启」；若仍失败，确认 23119
                端口未被占用，也可以用环境变量
                <code>PAPEREAD_ZOTERO</code> 指向其他地址。
              </li>
            </ol>
            <div>
              <button className="btn ghost" onClick={() => void connect()}>
                重新检测
              </button>
            </div>
          </div>
        ) : (
          <>
            <div className="zotero-toolbar">
              <select
                value={collection}
                onChange={(e) => setCollection(e.target.value)}
                title="按分类筛选"
              >
                <option value="">全部文献</option>
                {collections.map((col) => (
                  <option key={col.key} value={col.key}>
                    {col.name}
                    {col.items ? `（${col.items}）` : ''}
                  </option>
                ))}
              </select>
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="搜索 Zotero 里的标题、作者…"
              />
              {importable.length > 0 && (
                <button className="zotero-select-all" onClick={toggleAll}>
                  {allSelected ? '取消全选' : '全选本页'}
                </button>
              )}
              <button
                className="zotero-select-all"
                onClick={() => void doSync()}
                disabled={syncing || importing}
                title="把当前分类下所有带 PDF 的条目一次导入，已在库中的自动跳过"
              >
                {syncing ? '同步中…' : '同步全部'}
              </button>
            </div>

            {importing && (
              <div className="zotero-progress" title={`${progress.done}/${progress.total}`}>
                <span style={{ width: `${percent}%` }} />
              </div>
            )}
            {syncing && (
              <div className="zotero-progress" title="正在同步 Zotero 条目">
                <span style={{ width: '100%', opacity: 0.5 }} />
              </div>
            )}

            <div className="modal-body zotero-list">
              {result && (
                <div className={`zotero-result ${result.imported ? '' : 'warn'}`}>
                  已导入 {result.imported} 篇
                  {result.skipped ? `，跳过 ${result.skipped} 篇（无 PDF 或已在库中）` : ''}
                  {result.errors.length > 0 && (
                    <ul>
                      {result.errors.slice(0, 5).map((text) => (
                        <li key={text}>{text}</li>
                      ))}
                    </ul>
                  )}
                </div>
              )}

              {loading ? (
                <div className="zotero-empty">
                  <div className="spinner" />
                  正在读取条目…
                </div>
              ) : items.length === 0 ? (
                <div className="zotero-empty">
                  {query ? '没有匹配的条目' : 'Zotero 里还没有可导入的条目'}
                </div>
              ) : (
                <>
                  {items.map((item) => {
                    const disabled = !item.pdf || item.imported
                    return (
                      <label
                        key={item.key}
                        className={`zotero-row ${disabled ? 'is-disabled' : ''}`}
                        title={item.title}
                      >
                        <input
                          type="checkbox"
                          checked={selected.has(item.key)}
                          disabled={disabled || importing}
                          onChange={() => toggle(item.key)}
                        />
                        <div className="zotero-row-main">
                          <div className="zotero-row-title">{item.title}</div>
                          <div className="zotero-row-meta">
                            {item.authors || '作者未知'}
                            {item.year ? ` · ${item.year}` : ''}
                            {item.publication ? ` · ${item.publication}` : ''}
                          </div>
                        </div>
                        <div className="zotero-tags">
                          {item.imported ? (
                            <span className="zotero-tag ok">已在库中</span>
                          ) : item.pdf ? (
                            <span className="zotero-tag pdf">PDF</span>
                          ) : (
                            <span className="zotero-tag muted">无 PDF</span>
                          )}
                        </div>
                      </label>
                    )
                  })}
                  {hasMore && (
                    <button
                      className="zotero-more"
                      onClick={() => void loadItems(items.length, true)}
                      disabled={loadingMore}
                    >
                      {loadingMore ? '加载中…' : `加载更多（已显示 ${items.length}/${total}）`}
                    </button>
                  )}
                </>
              )}
            </div>

            <footer className="modal-foot zotero-foot">
              <span className="zotero-count">
                已选 {selected.size} 篇 · 本页 {items.length}/{total || items.length}
              </span>
              <button className="btn ghost" onClick={onClose} disabled={importing || syncing}>
                {result ? '完成' : '取消'}
              </button>
              <button
                className="btn primary"
                onClick={() => void doImport()}
                disabled={!selected.size || importing || syncing}
              >
                {importing
                  ? `导入中 ${Math.min(progress.done + 1, progress.total)}/${progress.total}…`
                  : selected.size
                    ? `导入 ${selected.size} 篇`
                    : '导入'}
              </button>
            </footer>
          </>
        )}
      </div>
    </div>
  )
}
