import { lazy, Suspense, useEffect, useMemo, useState } from 'react'
import { IconClose, IconDownload } from '../../components/icons'
import { Markdown } from '../../components/Markdown'
import {
  formatSize,
  researchApi,
  type ProjectFile,
} from '../../lib/researchApi'
import { SpreadsheetPreview } from './SpreadsheetPreview'

// pdf.js 体积较大，只有真正打开 PDF 时才加载。
const PdfViewer = lazy(() =>
  import('./PdfViewer').then((module) => ({ default: module.PdfViewer })),
)
// PPT 预览依赖解析库 + echarts，同样按需加载。
const PptxPreview = lazy(() =>
  import('./PptxPreview').then((module) => ({ default: module.PptxPreview })),
)
// Word 预览用 docx-preview，只在打开 .docx 时加载。
const DocxPreview = lazy(() =>
  import('./DocxPreview').then((module) => ({ default: module.DocxPreview })),
)

type PreviewKind =
  | 'image'
  | 'pdf'
  | 'pptx'
  | 'docx'
  | 'markdown'
  | 'csv'
  | 'sheet'
  | 'audio'
  | 'video'
  | 'code'
  | 'download'

const IMAGE_RE = /\.(png|jpe?g|gif|webp|svg|bmp|avif|ico)$/i
const MARKDOWN_RE = /\.(md|mdx|markdown)$/i
const CSV_RE = /\.(csv|tsv)$/i
const SHEET_RE = /\.(xlsx|xls|xlsm|ods)$/i
const PPTX_RE = /\.pptx$/i
const PPTX_MIME =
  'application/vnd.openxmlformats-officedocument.presentationml.presentation'
const DOCX_RE = /\.docx$/i
const DOCX_MIME =
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
const AUDIO_RE = /\.(mp3|wav|ogg|m4a|flac|aac)$/i
const VIDEO_RE = /\.(mp4|webm|mov|mkv|m4v)$/i
const CODE_RE =
  /\.(txt|text|json|jsonl|ya?ml|toml|xml|html?|css|scss|less|jsx?|tsx?|vue|svelte|py|r|jl|m|c|h|cpp|hpp|cc|cs|go|rs|java|kt|swift|php|rb|pl|lua|sh|bash|zsh|ps1|bat|cmd|sql|log|bib|tex|cfg|ini|conf|env|gitignore|csv)$/i

const KIND_LABEL: Record<PreviewKind, string> = {
  image: '图片',
  pdf: 'PDF',
  pptx: 'PPT',
  docx: 'Word',
  markdown: 'Markdown',
  csv: '表格',
  sheet: 'Excel',
  audio: '音频',
  video: '视频',
  code: '文本',
  download: '文件',
}

/** 按 MIME + 扩展名挑对应的预览器。 */
function detectKind(file: ProjectFile): PreviewKind {
  const name = file.name
  const mime = (file.mime || '').toLowerCase()
  if (mime.startsWith('image/') || IMAGE_RE.test(name)) return 'image'
  if (mime === 'application/pdf' || /\.pdf$/i.test(name)) return 'pdf'
  if (mime === PPTX_MIME || PPTX_RE.test(name)) return 'pptx'
  if (mime === DOCX_MIME || DOCX_RE.test(name)) return 'docx'
  if (mime === 'text/markdown' || MARKDOWN_RE.test(name)) return 'markdown'
  if (CSV_RE.test(name)) return 'csv'
  if (SHEET_RE.test(name)) return 'sheet'
  if (mime.startsWith('audio/') || AUDIO_RE.test(name)) return 'audio'
  if (mime.startsWith('video/') || VIDEO_RE.test(name)) return 'video'
  if (CODE_RE.test(name) || mime.startsWith('text/')) return 'code'
  return 'download'
}

/** 读取纯文本内容（Markdown / CSV / 代码共用）。 */
function useFileText(url: string, enabled: boolean) {
  const [text, setText] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(enabled)

  useEffect(() => {
    if (!enabled) return
    let alive = true
    setLoading(true)
    setError(null)
    setText('')
    fetch(url)
      .then((response) => {
        if (!response.ok) throw new Error(`读取失败（HTTP ${response.status}）`)
        return response.text()
      })
      .then((value) => {
        if (alive) setText(value)
      })
      .catch((loadError: unknown) => {
        if (alive) {
          setError(loadError instanceof Error ? loadError.message : '读取失败')
        }
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [url, enabled])

  return { text, error, loading }
}

const MAX_CODE_LINES = 4000

/** 代码 / 纯文本：等宽字体 + 行号。 */
function CodePreview({ text }: { text: string }) {
  const lines = useMemo(() => text.split('\n'), [text])
  const shown = lines.slice(0, MAX_CODE_LINES)
  return (
    <div className="code-preview">
      <pre className="code-preview-body">
        {shown.map((line, index) => (
          <span className="code-preview-line" key={index}>
            <i>{index + 1}</i>
            <code>{line || ' '}</code>
          </span>
        ))}
      </pre>
      {lines.length > MAX_CODE_LINES && (
        <p className="code-preview-note">
          文件较长，仅显示前 {MAX_CODE_LINES} 行，可下载查看完整内容。
        </p>
      )}
    </div>
  )
}

interface Props {
  projectId: string
  file: ProjectFile
  onClose: () => void
}

/** 文件预览：按类型分派到图片 / PDF / Markdown / 表格 / 音视频 / 代码预览器。 */
export function FilePreview({ projectId, file, onClose }: Props) {
  const url = researchApi.fileContentUrl(projectId, file.id)
  const kind = detectKind(file)
  const needsText =
    kind === 'markdown' || kind === 'code'
  const { text, error, loading } = useFileText(url, needsText)

  return (
    <div className="project-preview">
      <header className="project-preview-head">
        <div>
          <h2>{file.name}</h2>
          <span>
            {file.path}
            {file.size ? ` · ${formatSize(file.size)}` : ''} · {KIND_LABEL[kind]}
          </span>
        </div>
        <div className="project-preview-actions">
          <a className="icon-btn" href={url} download={file.name} title="下载">
            <IconDownload />
          </a>
          <button className="icon-btn" title="关闭预览" onClick={onClose}>
            <IconClose />
          </button>
        </div>
      </header>

      <div className="project-preview-body" data-kind={kind}>
        {kind === 'image' ? (
          <div className="project-preview-image-wrap">
            <img src={url} alt={file.name} className="project-preview-image" />
          </div>
        ) : kind === 'pdf' ? (
          <Suspense
            fallback={<p className="project-preview-note">正在加载 PDF 组件…</p>}
          >
            <PdfViewer url={url} title={file.name} />
          </Suspense>
        ) : kind === 'pptx' ? (
          <Suspense
            fallback={<p className="project-preview-note">正在加载 PPT 组件…</p>}
          >
            <PptxPreview url={url} name={file.name} />
          </Suspense>
        ) : kind === 'docx' ? (
          <Suspense
            fallback={<p className="project-preview-note">正在加载 Word 组件…</p>}
          >
            <DocxPreview url={url} name={file.name} />
          </Suspense>
        ) : kind === 'csv' || kind === 'sheet' ? (
          <SpreadsheetPreview url={url} name={file.name} kind={kind} />
        ) : kind === 'audio' ? (
          <div className="project-preview-media-wrap">
            <audio controls src={url} className="project-preview-media" />
          </div>
        ) : kind === 'video' ? (
          <div className="project-preview-media-wrap">
            <video controls src={url} className="project-preview-media" />
          </div>
        ) : kind === 'markdown' ? (
          error ? (
            <p className="project-preview-note">{error}</p>
          ) : loading ? (
            <p className="project-preview-note">正在读取文件…</p>
          ) : (
            <div className="project-preview-markdown">
              <Markdown text={text} />
            </div>
          )
        ) : kind === 'code' ? (
          error ? (
            <p className="project-preview-note">{error}</p>
          ) : loading ? (
            <p className="project-preview-note">正在读取文件…</p>
          ) : (
            <CodePreview text={text} />
          )
        ) : (
          <div className="project-preview-note">
            暂不支持内嵌预览，请
            <a href={url} download={file.name}>
              下载文件
            </a>
            。
          </div>
        )}
      </div>
    </div>
  )
}
