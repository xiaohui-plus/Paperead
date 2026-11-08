import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  DEMO_PAPER,
  deletePaper,
  importZotero,
  listPapers,
  reparsePaper,
  uploadPapers,
  type PaperRow,
} from '../lib/library'
import { PaperCard } from './PaperCard'
import { Sidebar } from './Sidebar'
import { SettingsDialog } from './SettingsDialog'
import { IconGrid, IconRows, IconSettings, IconUpload } from './icons'

type Filter = 'all' | 'ready' | 'untranslated' | 'failed'
type Sort = 'recent' | 'title' | 'pages'

interface Props {
  onOpenPaper: (id: string) => void
}

export function LibraryPage({ onOpenPaper }: Props) {
  const [papers, setPapers] = useState<PaperRow[]>([])
  const [loading, setLoading] = useState(true)
  const [demoMode, setDemoMode] = useState(false)
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState<Filter>('all')
  const [sort, setSort] = useState<Sort>('recent')
  const [view, setView] = useState<'grid' | 'list'>('grid')
  const [dragOver, setDragOver] = useState(false)
  const [toast, setToast] = useState<{ text: string; kind: 'ok' | 'err' } | null>(null)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [uploading, setUploading] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  const notify = useCallback((text: string, kind: 'ok' | 'err' = 'ok') => {
    setToast({ text, kind })
    window.setTimeout(() => setToast(null), 4200)
  }, [])

  const refresh = useCallback(async () => {
    try {
      const rows = await listPapers()
      setPapers(rows)
      setDemoMode(false)
    } catch {
      // 后端没起：退回内置示例，页面依然可看
      setPapers([DEMO_PAPER])
      setDemoMode(true)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  // 有解析/翻译中的任务时轮询
  useEffect(() => {
    if (demoMode) return
    const active = papers.some((p) => p.status === 'parsing')
    if (!active) return
    const timer = window.setInterval(() => void refresh(), 1800)
    return () => window.clearInterval(timer)
  }, [papers, demoMode, refresh])

  const doUpload = async (files: File[]) => {
    const pdfs = files.filter((f) => f.name.toLowerCase().endsWith('.pdf'))
    if (!pdfs.length) {
      notify('只支持 PDF 文件', 'err')
      return
    }
    setUploading(true)
    try {
      const res = await uploadPapers(pdfs)
      await refresh()
      if (res.added.length > 0) {
        notify(`已添加 ${res.added.length} 篇，正在解析…`)
        // 上传后直接进入阅读页；解析没完成时阅读页会自动重试
        onOpenPaper(res.added[0].id)
      } else {
        notify(`上传失败：${res.failed[0]?.error ?? '未知错误'}`, 'err')
      }
    } catch (e) {
      notify(e instanceof Error ? e.message : '上传失败', 'err')
    } finally {
      setUploading(false)
    }
  }

  const onDrop = (e: React.DragEvent) => {
    e.preventDefault()
    setDragOver(false)
    void doUpload(Array.from(e.dataTransfer.files))
  }

  const onDelete = async (paper: PaperRow) => {
    if (!window.confirm(`删除《${paper.title}》？`)) return
    try {
      await deletePaper(paper.id)
      await refresh()
      notify('已删除')
    } catch (e) {
      notify(e instanceof Error ? e.message : '删除失败', 'err')
    }
  }

  const onZotero = async () => {
    try {
      const res = await importZotero(20)
      notify(`从 Zotero 导入 ${res.imported} 篇（跳过 ${res.skipped} 篇无 PDF）`)
      await refresh()
    } catch (e) {
      notify(e instanceof Error ? e.message : 'Zotero 导入失败', 'err')
    }
  }

  const onReparse = async (paper: PaperRow) => {
    try {
      await reparsePaper(paper.id)
      notify('已重新开始解析')
      await refresh()
    } catch (e) {
      notify(e instanceof Error ? e.message : '解析失败', 'err')
    }
  }

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase()
    let rows = papers.filter((p) => {
      if (q) {
        const hay = `${p.title} ${p.authors} ${p.filename}`.toLowerCase()
        if (!hay.includes(q)) return false
      }
      if (filter === 'ready') return p.status === 'ready'
      if (filter === 'untranslated') return p.translated === 0 && p.status === 'ready'
      if (filter === 'failed') return p.status === 'failed'
      return true
    })
    rows = [...rows].sort((a, b) => {
      if (sort === 'title') return a.title.localeCompare(b.title, 'zh')
      if (sort === 'pages') return b.pages - a.pages
      return b.created_at - a.created_at
    })
    return rows
  }, [papers, query, filter, sort])

  return (
    <div
      className="library"
      onDragOver={(e) => {
        e.preventDefault()
        setDragOver(true)
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={onDrop}
    >
      <Sidebar active="library" count={papers.length} onSettings={() => setSettingsOpen(true)} />

      <main className="library-main">
        <header className="library-head">
          <h1>文献库</h1>
          <div className="library-head-actions">
            <button className="btn ghost" onClick={onZotero} title="从本机 Zotero 导入带 PDF 的条目">
              从 Zotero 导入
            </button>
            <button className="btn primary" onClick={() => inputRef.current?.click()} disabled={uploading}>
              <IconUpload /> {uploading ? '上传中…' : '添加文章'}
            </button>
            <input
              ref={inputRef}
              type="file"
              accept="application/pdf"
              multiple
              hidden
              onChange={(e) => {
                void doUpload(Array.from(e.target.files ?? []))
                e.target.value = ''
              }}
            />
          </div>
        </header>

        {demoMode && (
          <div className="library-notice">
            本地服务未启动，当前展示内置示例。
            启动方式：<code>python -m server.main</code>（另开终端）
          </div>
        )}

        <div className="library-search">
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索标题、作者或文件名…"
          />
          <span className="search-count">{visible.length} 篇</span>
        </div>

        <div className="library-toolbar">
          <label className="tool">
            <span>筛选</span>
            <select value={filter} onChange={(e) => setFilter(e.target.value as Filter)}>
              <option value="all">全部</option>
              <option value="ready">已就绪</option>
              <option value="untranslated">未翻译</option>
              <option value="failed">解析失败</option>
            </select>
          </label>
          <label className="tool">
            <span>排序</span>
            <select value={sort} onChange={(e) => setSort(e.target.value as Sort)}>
              <option value="recent">最近添加</option>
              <option value="title">标题</option>
              <option value="pages">页数</option>
            </select>
          </label>
          <div className="tool view-toggle" role="group" aria-label="视图">
            <button data-active={view === 'grid'} onClick={() => setView('grid')} title="网格">
              <IconGrid />
            </button>
            <button data-active={view === 'list'} onClick={() => setView('list')} title="列表">
              <IconRows />
            </button>
          </div>
          <button className="tool icon-only" onClick={() => setSettingsOpen(true)} title="设置">
            <IconSettings />
          </button>
        </div>

        {loading ? (
          <div className="library-empty">正在加载…</div>
        ) : visible.length === 0 ? (
          <div className="library-empty">
            {papers.length === 0 ? (
              <>
                <div className="dropzone" onClick={() => inputRef.current?.click()}>
                  <IconUpload />
                  <b>把 PDF 拖到这里，或点击上传</b>
                  <span>支持多选。上传后会自动解析版面、图表和公式，随后可一键翻译。</span>
                </div>
              </>
            ) : (
              <span>没有符合条件的文章</span>
            )}
          </div>
        ) : (
          <div className={`paper-grid ${view === 'list' ? 'is-list' : ''}`}>
            {visible.map((p) => (
              <PaperCard
                key={p.id}
                paper={p}
                view={view}
                onOpen={onOpenPaper}
                onDelete={onDelete}
                onReparse={onReparse}
              />
            ))}
            <button
              className="paper-card add-card"
              onClick={() => inputRef.current?.click()}
              title="添加文章"
            >
              <IconUpload />
              <span>添加文章</span>
            </button>
          </div>
        )}

        {visible.length > 0 && <p className="library-end">- 没有更多文章了 -</p>}
      </main>

      {dragOver && (
        <div className="drop-overlay">
          <div>
            <IconUpload />
            松开即可上传 PDF
          </div>
        </div>
      )}

      {toast && (
        <div className={`toast ${toast.kind}`} onClick={() => setToast(null)}>
          {toast.text}
        </div>
      )}

      <SettingsDialog
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        onSaved={() => notify('已保存模型配置')}
      />
    </div>
  )
}
