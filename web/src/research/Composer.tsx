import { useEffect, useRef, useState } from 'react'
import { ModelPicker } from '../components/ModelPicker'
import { IconClose, IconPlus, IconSend, IconStop } from '../components/icons'
import {
  researchApi,
  type ProjectFile,
} from '../lib/researchApi'
import {
  listResearchModels,
  selectedResearchModel,
  setResearchModel,
  syncResearchModel,
} from '../lib/researchModel'
import type { ModelProfile } from '../lib/settings'
import { tourBus } from '../tour/tourBus'

interface Props {
  running: boolean
  placeholder?: string
  autoFocus?: boolean
  hint?: string
  /** 附件上传的目标项目；不传则隐藏上传按钮。 */
  projectId?: string
  onUploaded?: (file: ProjectFile) => void
  onSend: (text: string) => void
  onStop: () => void
}

function sizeText(size: number): string {
  if (size >= 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`
  if (size >= 1024) return `${Math.round(size / 1024)} KB`
  return `${size} B`
}

/** 会话输入框：Enter 发送、Shift+Enter 换行，输入法组合期间不发送。 */
export function Composer({
  running,
  placeholder = '描述你想研究的问题…',
  autoFocus = false,
  hint,
  projectId,
  onUploaded,
  onSend,
  onStop,
}: Props) {
  const [draft, setDraft] = useState('')
  const textarea = useRef<HTMLTextAreaElement>(null)
  const fileInput = useRef<HTMLInputElement>(null)
  const [models, setModels] = useState<ModelProfile[]>(() => listResearchModels())
  const [model, setModel] = useState<ModelProfile | null>(() => selectedResearchModel())
  const [attachments, setAttachments] = useState<ProjectFile[]>([])
  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState('')
  const [dragging, setDragging] = useState(false)
  // 手机端回车是换行键，不劫持成发送。
  const [touch] = useState(
    () =>
      typeof window !== 'undefined' &&
      Boolean(window.matchMedia?.('(pointer: coarse)').matches),
  )

  useEffect(() => {
    if (autoFocus) textarea.current?.focus()
  }, [autoFocus])

  // 进入输入框时把当前选择同步给本地服务，保证 agent 用对模型
  useEffect(() => {
    setModels(listResearchModels())
    setModel(selectedResearchModel())
    void syncResearchModel()
  }, [])

  const chooseModel = (next: ModelProfile) => {
    setResearchModel(next)
    setModel(next)
    tourBus.emit('composer:model-changed', { model: next.label })
  }

  const addFiles = async (files: File[]) => {
    if (!projectId || files.length === 0) return
    setUploading(true)
    setUploadError('')
    const added: ProjectFile[] = []
    for (const file of files) {
      try {
        const item = await researchApi.uploadFile(projectId, file, null)
        added.push(item)
        onUploaded?.(item)
        tourBus.emit('composer:attachment-added', { file: item.name })
      } catch (error) {
        setUploadError(error instanceof Error ? error.message : '上传失败')
      }
    }
    if (added.length > 0) setAttachments((prev) => [...prev, ...added])
    setUploading(false)
  }

  const submit = () => {
    const text = draft.trim()
    if (!text) return
    const withFiles =
      attachments.length > 0
        ? `${text}\n\n附件（已保存在项目目录，可直接读取）：\n${attachments
            .map((item) => `- ${item.path}`)
            .join('\n')}`
        : text
    setDraft('')
    setAttachments([])
    setUploadError('')
    tourBus.emit('composer:message-sent', { text })
    onSend(withFiles)
  }

  return (
    <div
      className={`project-composer research-chat-composer${
        dragging ? ' is-dragging' : ''
      }`}
      data-tour="composer"
      onDragOver={(event) => {
        if (!projectId) return
        event.preventDefault()
        setDragging(true)
      }}
      onDragLeave={(event) => {
        if (event.currentTarget.contains(event.relatedTarget as Node)) return
        setDragging(false)
      }}
      onDrop={(event) => {
        if (!projectId) return
        event.preventDefault()
        setDragging(false)
        void addFiles(Array.from(event.dataTransfer.files))
      }}
    >
      {attachments.length > 0 && (
        <div className="composer-attachments">
          {attachments.map((item) => (
            <span className="attachment-chip" key={item.id} title={item.path}>
              <span className="attachment-name">{item.name}</span>
              <span className="attachment-size">{sizeText(item.size)}</span>
              <button
                type="button"
                className="attachment-remove"
                title="移除附件"
                onClick={() =>
                  setAttachments((prev) => prev.filter((one) => one.id !== item.id))
                }
              >
                <IconClose />
              </button>
            </span>
          ))}
        </div>
      )}
      {(uploading || uploadError) && (
        <div className={`composer-upload-note${uploadError ? ' is-error' : ''}`}>
          {uploadError || '正在上传附件…'}
        </div>
      )}
      <textarea
        ref={textarea}
        value={draft}
        placeholder={placeholder}
        rows={2}
        onChange={(event) => setDraft(event.target.value)}
        onPaste={(event) => {
          const files = Array.from(event.clipboardData.files)
          if (files.length > 0 && projectId) {
            event.preventDefault()
            void addFiles(files)
          }
        }}
        onKeyDown={(event) => {
          if (event.nativeEvent.isComposing) return
          if (event.key === 'Enter' && !event.shiftKey && !touch) {
            event.preventDefault()
            submit()
          }
        }}
      />
      <div className="project-composer-actions">
        <div className="composer-tools" data-tour="composer-tools">
          {projectId && (
            <>
              <input
                ref={fileInput}
                type="file"
                multiple
                hidden
                onChange={(event) => {
                  void addFiles(Array.from(event.target.files ?? []))
                  event.target.value = ''
                }}
              />
              <button
                type="button"
                className="composer-add"
                title="上传文件到项目目录"
                disabled={uploading}
                onClick={() => fileInput.current?.click()}
              >
                <IconPlus />
              </button>
            </>
          )}
          <ModelPicker models={models} current={model} onChoose={chooseModel} />
        </div>
        <span className="composer-hint">
          {hint ??
            (running
              ? '正在研究，可随时停止'
              : touch
                ? '点「发送」开始，回车换行'
                : 'Enter 发送 · Shift+Enter 换行')}
        </span>
        {running ? (
          <button className="research-stop" onClick={onStop}>
            <IconStop />
            停止
          </button>
        ) : (
          <button
            className="research-send"
            disabled={!draft.trim()}
            onClick={submit}
          >
            <IconSend />
            发送
          </button>
        )}
      </div>
    </div>
  )
}
