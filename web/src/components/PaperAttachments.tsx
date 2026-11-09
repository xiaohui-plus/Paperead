import { useEffect, useRef, useState } from 'react'
import {
  deletePaperAttachment,
  listPaperAttachments,
  uploadPaperAttachment,
  type PaperAttachment,
} from '../lib/library'
import { IconClose, IconFolder, IconPlus } from './icons'

function sizeText(size: number): string {
  if (size >= 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`
  if (size >= 1024) return `${Math.round(size / 1024)} KB`
  return `${size} B`
}

/** 阅读器「附件」页签：这篇论文的附件库，可上传、预览、删除。 */
export function PaperAttachments({ paperId }: { paperId: string }) {
  const [items, setItems] = useState<PaperAttachment[]>([])
  const [loading, setLoading] = useState(true)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState('')
  const [dragging, setDragging] = useState(false)
  const input = useRef<HTMLInputElement>(null)

  const load = async () => {
    try {
      setItems(await listPaperAttachments(paperId))
      setError('')
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : '读取附件失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    setLoading(true)
    void load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [paperId])

  const addFiles = async (files: File[]) => {
    if (files.length === 0 || uploading) return
    setUploading(true)
    setError('')
    for (const file of files.slice(0, 6)) {
      try {
        await uploadPaperAttachment(paperId, file)
      } catch (uploadError) {
        setError(
          uploadError instanceof Error ? uploadError.message : '附件上传失败',
        )
      }
    }
    setUploading(false)
    await load()
  }

  const remove = async (item: PaperAttachment) => {
    if (!window.confirm(`删除附件「${item.name}」？`)) return
    try {
      await deletePaperAttachment(paperId, item.name)
      setItems((prev) => prev.filter((one) => one.name !== item.name))
    } catch (deleteError) {
      setError(deleteError instanceof Error ? deleteError.message : '删除失败')
    }
  }

  const total = items.reduce((sum, item) => sum + item.size, 0)

  return (
    <div
      className={`tab-body paper-files${dragging ? ' is-dragging' : ''}`}
      onDragOver={(event) => {
        event.preventDefault()
        setDragging(true)
      }}
      onDragLeave={(event) => {
        if (event.currentTarget.contains(event.relatedTarget as Node)) return
        setDragging(false)
      }}
      onDrop={(event) => {
        event.preventDefault()
        setDragging(false)
        void addFiles(Array.from(event.dataTransfer.files))
      }}
    >
      <header className="paper-files-head">
        <div>
          <h3>论文附件</h3>
          <p>
            上传的补充材料按这篇论文保存，问答时可直接勾选引用。
            {items.length > 0 && ` 共 ${items.length} 个，合计 ${sizeText(total)}。`}
          </p>
        </div>
        <button
          className="btn small"
          type="button"
          disabled={uploading}
          onClick={() => input.current?.click()}
        >
          <IconPlus />
          {uploading ? '上传中…' : '上传文件'}
        </button>
      </header>
      <input
        ref={input}
        type="file"
        multiple
        hidden
        onChange={(event) => {
          void addFiles(Array.from(event.target.files ?? []))
          event.target.value = ''
        }}
      />

      {error && <div className="paper-files-error">{error}</div>}

      {loading ? (
        <div className="paper-files-empty">正在读取附件…</div>
      ) : items.length === 0 ? (
        <div className="paper-files-empty">
          <IconFolder />
          <span>还没有附件。可以点右上角「上传文件」，或把文件拖到这里。</span>
        </div>
      ) : (
        <ul className="paper-files-list">
          {items.map((item) => (
            <li className="paper-file-row" key={item.name}>
              <a
                className="paper-file-name"
                href={item.url}
                target="_blank"
                rel="noreferrer"
                title="在新标签页预览"
              >
                {item.name}
              </a>
              <span className="paper-file-meta">
                {sizeText(item.size)}
                {' · '}
                {item.textChars
                  ? `可引用 ${item.textChars.toLocaleString()} 字`
                  : '仅文件名'}
              </span>
              <button
                className="paper-file-remove"
                type="button"
                title="删除附件"
                onClick={() => void remove(item)}
              >
                <IconClose />
              </button>
            </li>
          ))}
        </ul>
      )}

      <p className="paper-files-hint">
        图片和扫描件暂不做 OCR，只能按文件名参考；PDF、docx 与文本文件会抽取正文进入问答。
      </p>
    </div>
  )
}
