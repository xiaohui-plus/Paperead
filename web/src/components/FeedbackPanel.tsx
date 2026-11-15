import { useEffect, useRef, useState } from 'react'
import {
  clearFeedbackToken,
  getFeedbackStatus,
  saveFeedbackToken,
  submitFeedback,
  type FeedbackResult,
  type FeedbackStatus,
} from '../lib/feedback'
import { IconClose, IconImage, IconSend } from './icons'

const MAX_IMAGES = 6
const MAX_BYTES = 8 * 1024 * 1024
const ALLOWED_TYPES = ['image/png', 'image/jpeg', 'image/gif', 'image/webp']
const TOKEN_PAGE = 'https://gitee.com/profile/personal_access_tokens'

interface DraftImage {
  id: string
  name: string
  type: string
  size: number
  data: string
  url: string
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : '提交失败，请稍后重试'
}

function sizeText(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)}MB`
  if (bytes >= 1024) return `${Math.round(bytes / 1024)}KB`
  return `${bytes}B`
}

function readAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result || ''))
    reader.onerror = () => reject(new Error('图片读取失败'))
    reader.readAsDataURL(file)
  })
}

function sourceLabel(source: FeedbackStatus['source'] | undefined): string {
  if (source === 'builtin') return '内置提交通道'
  if (source === 'env') return '环境变量令牌'
  if (source === 'user') return '你的令牌'
  return '未配置'
}

/** 设置 → 问题反馈：提交标题、描述与截图到 Gitee Issue。 */
export function FeedbackPanel() {
  const [status, setStatus] = useState<FeedbackStatus | null>(null)
  const [statusError, setStatusError] = useState('')
  const [title, setTitle] = useState('')
  const [body, setBody] = useState('')
  const [images, setImages] = useState<DraftImage[]>([])
  const [dragging, setDragging] = useState(false)
  const [sending, setSending] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [result, setResult] = useState<FeedbackResult | null>(null)
  const [token, setToken] = useState('')
  const [savingToken, setSavingToken] = useState(false)
  const [tokenError, setTokenError] = useState('')
  const fileRef = useRef<HTMLInputElement>(null)
  const titleRef = useRef<HTMLInputElement>(null)
  const imagesRef = useRef<DraftImage[]>([])

  useEffect(() => {
    imagesRef.current = images
  }, [images])

  useEffect(() => {
    return () => {
      imagesRef.current.forEach((item) => URL.revokeObjectURL(item.url))
    }
  }, [])

  const loadStatus = async () => {
    setStatusError('')
    try {
      setStatus(await getFeedbackStatus())
    } catch (loadError) {
      setStatusError(errorText(loadError))
    }
  }

  useEffect(() => {
    void loadStatus()
  }, [])

  const addFiles = async (files: File[]) => {
    if (!files.length) return
    setError('')
    setNotice('')
    const accepted: File[] = []
    let slots = MAX_IMAGES - imagesRef.current.length
    for (const file of files) {
      if (!ALLOWED_TYPES.includes(file.type)) {
        setError(`「${file.name || '图片'}」格式不支持，请使用 PNG / JPG / GIF / WebP`)
        continue
      }
      if (file.size > MAX_BYTES) {
        setError(`「${file.name || '图片'}」有 ${sizeText(file.size)}，单张不能超过 8MB`)
        continue
      }
      if (slots <= 0) {
        setError(`最多上传 ${MAX_IMAGES} 张图片`)
        break
      }
      accepted.push(file)
      slots -= 1
    }
    if (!accepted.length) return

    const next: DraftImage[] = []
    for (const file of accepted) {
      try {
        const data = await readAsDataUrl(file)
        next.push({
          id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
          name: file.name || '截图',
          type: file.type,
          size: file.size,
          data,
          url: URL.createObjectURL(file),
        })
      } catch (readError) {
        setError(errorText(readError))
      }
    }
    if (next.length) setImages((prev) => [...prev, ...next].slice(0, MAX_IMAGES))
  }

  const removeImage = (id: string) => {
    setImages((prev) => {
      const target = prev.find((item) => item.id === id)
      if (target) URL.revokeObjectURL(target.url)
      return prev.filter((item) => item.id !== id)
    })
  }

  const onPaste = (event: React.ClipboardEvent) => {
    const files = Array.from(event.clipboardData.files || []).filter((file) =>
      file.type.startsWith('image/'),
    )
    if (!files.length) return
    event.preventDefault()
    const text = event.clipboardData.getData('text/plain')
    if (text) setBody((prev) => (prev ? `${prev}\n${text}` : text))
    void addFiles(files)
  }

  const submit = async () => {
    if (sending) return
    if (!title.trim()) {
      setError('请先填写问题标题')
      titleRef.current?.focus()
      return
    }
    setSending(true)
    setError('')
    setNotice('')
    setResult(null)
    try {
      const payload = {
        title: title.trim(),
        body: body.trim(),
        images: images.map(({ name, type, data }) => ({ name, type, data })),
      }
      const created = await submitFeedback(payload)
      setResult(created)
      imagesRef.current.forEach((item) => URL.revokeObjectURL(item.url))
      setImages([])
      setTitle('')
      setBody('')
    } catch (submitError) {
      setError(errorText(submitError))
    } finally {
      setSending(false)
    }
  }

  const copyAndOpen = async () => {
    const text = [
      title.trim() ? `# ${title.trim()}` : '',
      body.trim(),
      images.length ? `（另有 ${images.length} 张截图，请在网页里手动拖入）` : '',
    ]
      .filter(Boolean)
      .join('\n\n')
    try {
      await navigator.clipboard.writeText(text)
      setNotice('内容已复制，打开 Gitee 后直接粘贴即可。')
    } catch {
      setNotice('复制失败，请手动选中文本复制。')
    }
  }

  const saveToken = async () => {
    if (savingToken) return
    setSavingToken(true)
    setTokenError('')
    try {
      const next = await saveFeedbackToken(token.trim())
      setStatus(next)
      setToken('')
      setNotice('令牌已保存，之后可以直接在应用内提交。')
    } catch (saveError) {
      setTokenError(errorText(saveError))
    } finally {
      setSavingToken(false)
    }
  }

  const dropToken = async () => {
    setTokenError('')
    try {
      setStatus(await clearFeedbackToken())
      setNotice('已清除自定义令牌。')
    } catch (clearError) {
      setTokenError(errorText(clearError))
    }
  }

  const configured = Boolean(status?.configured)
  const hint = error ? (
    <span className="feedback-hint is-error">{error}</span>
  ) : notice ? (
    <span className="feedback-hint is-ok">{notice}</span>
  ) : (
    <span className="feedback-hint">
      {statusError
        ? `读取提交状态失败：${statusError}`
        : configured
          ? '截图会上传到公开的 Gitee 仓库，请勿包含隐私信息。'
          : '尚未配置提交令牌；可以先复制内容，在 Gitee 网页里提交。'}
    </span>
  )

  return (
    <div className="modal-body feedback-panel" onPaste={onPaste}>
      <section className="reading-card">
        <div className="reading-card-head">
          <div>
            <h3>问题反馈</h3>
            <p>只提交你填写的标题、描述与截图，不会附带论文内容、密钥或日志。</p>
          </div>
          <span className="reading-badge" data-off={!configured}>
            {status ? sourceLabel(status.source) : '检查中…'}
          </span>
        </div>

        <div className="reading-row">
          <div className="reading-row-label">
            <b>标题</b>
            <span>一句话说清问题，例如「导出 PDF 时页码错位」</span>
          </div>
          <input
            ref={titleRef}
            className="input feedback-title"
            value={title}
            maxLength={120}
            placeholder="问题标题"
            onChange={(event) => setTitle(event.target.value)}
          />
        </div>

        <div className="feedback-text">
          <div className="feedback-text-head">
            <b>描述</b>
            <span>复现步骤、期望结果都可以写在这里</span>
          </div>
          <textarea
            value={body}
            maxLength={20000}
            placeholder="例如：在阅读页选中一段文字后点荧光笔，颜色面板会立刻收起。期望：鼠标移到颜色上仍保持展开。"
            onChange={(event) => setBody(event.target.value)}
          />
        </div>

        <div
          className="feedback-drop"
          data-active={dragging}
          role="button"
          tabIndex={0}
          onClick={() => fileRef.current?.click()}
          onKeyDown={(event) => {
            if (event.key === 'Enter' || event.key === ' ') fileRef.current?.click()
          }}
          onDragOver={(event) => {
            event.preventDefault()
            setDragging(true)
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(event) => {
            event.preventDefault()
            setDragging(false)
            void addFiles(Array.from(event.dataTransfer.files))
          }}
        >
          <IconImage />
          <div>
            <b>粘贴截图</b>
            <span>在输入框里按 Ctrl+V，或把图片拖到这里 / 点击选择（{images.length}/{MAX_IMAGES}）</span>
          </div>
        </div>

        {images.length > 0 && (
          <div className="feedback-shots">
            {images.map((item) => (
              <figure className="feedback-shot" key={item.id}>
                <img src={item.url} alt={item.name} />
                <button
                  type="button"
                  title="移除这张截图"
                  aria-label="移除这张截图"
                  onClick={() => removeImage(item.id)}
                >
                  <IconClose />
                </button>
                <figcaption>{sizeText(item.size)}</figcaption>
              </figure>
            ))}
          </div>
        )}

        <input
          ref={fileRef}
          type="file"
          accept="image/png,image/jpeg,image/gif,image/webp"
          multiple
          hidden
          onChange={(event) => {
            void addFiles(Array.from(event.target.files || []))
            event.target.value = ''
          }}
        />
      </section>

      {result && (
        <div className="feedback-result">
          <b>已提交</b>
          <span>
            Issue #{result.number}
            {result.images ? ` · ${result.images} 张截图` : ''}
          </span>
          <a href={result.issue_url} target="_blank" rel="noreferrer">
            在 Gitee 查看
          </a>
        </div>
      )}

      <div className="feedback-actions">
        {hint}
        {(!configured || error) && (
          <button type="button" className="feedback-link" onClick={() => void copyAndOpen()}>
            复制内容
          </button>
        )}
        {(!configured || error) && status && (
          <a
            className="feedback-link"
            href={`${status.issue_page}/new`}
            target="_blank"
            rel="noreferrer"
          >
            打开 Gitee 新建 Issue
          </a>
        )}
        <button
          type="button"
          className="btn primary feedback-submit"
          disabled={sending || !configured}
          onClick={() => void submit()}
        >
          <IconSend />
          {sending ? '提交中…' : '提交到 Gitee'}
        </button>
      </div>

      <details className="feedback-token" open={!configured}>
        <summary>提交令牌</summary>
        <div className="feedback-token-body">
          <p>
            当前使用{status ? sourceLabel(status.source) : '（读取中）'}。令牌只保存在本机数据目录，
            不会上传或随安装包分发；内置通道不可用时，可以换成你自己的令牌。
          </p>
          <div className="feedback-token-row">
            <input
              className="input"
              type="password"
              value={token}
              placeholder="填写你自己的 Gitee 私人令牌"
              onChange={(event) => setToken(event.target.value)}
            />
            <button
              type="button"
              className="btn"
              disabled={savingToken || token.trim().length < 16}
              onClick={() => void saveToken()}
            >
              {savingToken ? '验证中…' : '保存'}
            </button>
            {status?.source === 'user' && (
              <button type="button" className="btn ghost" onClick={() => void dropToken()}>
                清除
              </button>
            )}
          </div>
          {tokenError && <p className="feedback-token-error">{tokenError}</p>}
          <ol className="feedback-steps">
            <li>
              打开{' '}
              <a href={TOKEN_PAGE} target="_blank" rel="noreferrer">
                Gitee 私人令牌
              </a>{' '}
              页面并登录。
            </li>
            <li>
              生成新令牌，勾选 <code>issues</code> 权限即可（创建 Issue 只需要这一项）。
            </li>
            <li>复制令牌填入上方，点「保存」验证通过后即可在应用内提交反馈。</li>
          </ol>
        </div>
      </details>
    </div>
  )
}