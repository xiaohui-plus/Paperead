import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { ModelPicker } from '../components/ModelPicker'
import {
  IconArrowUp,
  IconAt,
  IconChevronDown,
  IconClose,
  IconPlus,
  IconShield,
  IconSlash,
  IconStop,
} from '../components/icons'
import {
  researchApi,
  type PermissionPreset,
  type ProjectFile,
  type ProjectKind,
  type ResearchCommand,
  type ResearchMode,
  type SendMeta,
  type WorkspaceScope,
} from '../lib/researchApi'
import {
  listResearchModels,
  selectedResearchModel,
  setResearchModel,
  syncResearchModel,
} from '../lib/researchModel'
import type { AgentEvent } from '../lib/researchTypes'
import type { ModelProfile } from '../lib/settings'
import { tourBus } from '../tour/tourBus'

interface Props {
  running: boolean
  placeholder?: string
  autoFocus?: boolean
  hint?: string
  kind: ProjectKind
  /** 附件上传的目标项目；不传则隐藏上传按钮。 */
  projectId?: string
  /** 有会话才能执行 / 指令与列出命令。 */
  sessionId?: string
  onUploaded?: (file: ProjectFile) => void
  onSend: (text: string, meta: SendMeta) => void
  onStop: () => void
  onCommandResult?: (event: AgentEvent) => void
  variant?: 'session' | 'home'
  /** 快捷卡片填充：key 变化时把 text 放进输入框。 */
  seed?: { key: number; text: string }
  /** 当前会话的权限预设（事件流同步）；为空表示该会话还没有记录。 */
  permission?: PermissionPreset | null
  /** 空会话时把本地权限偏好下发到该会话。 */
  empty?: boolean
}

interface Mention {
  key: string
  kind: 'file' | 'paper'
  name: string
  path?: string
  paperId?: string
}

const MODE_KEY = 'paperead.research.composer'

function loadPrefs(): { mode: ResearchMode; permission: PermissionPreset } {
  try {
    const raw = localStorage.getItem(MODE_KEY)
    if (raw) {
      const value = JSON.parse(raw) as { mode?: string; permission?: string }
      const permission: PermissionPreset =
        value.permission === 'read-only' || value.permission === 'danger-full-access'
          ? value.permission
          : 'workspace-write'
      return {
        mode: value.mode === 'deep' ? 'deep' : 'standard',
        permission,
      }
    }
  } catch {
    // 忽略损坏的本地配置
  }
  return { mode: 'standard', permission: 'workspace-write' }
}

function savePrefs(mode: ResearchMode, permission: PermissionPreset) {
  try {
    localStorage.setItem(MODE_KEY, JSON.stringify({ mode, permission }))
  } catch {
    // 隐私模式下写入失败不影响当前会话
  }
}

const PERMISSION_LABELS: Record<PermissionPreset, string> = {
  'read-only': '只读分析',
  'workspace-write': '沙箱执行',
  'danger-full-access': '完全权限',
}

function sizeText(size: number): string {
  if (size >= 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`
  if (size >= 1024) return `${Math.round(size / 1024)} KB`
  return `${size} B`
}

/** 研究/综述输入框：模式 + 附件 + @ 引用 + / 指令 + 模型 + 圆形发送。 */
export function Composer({
  running,
  placeholder,
  autoFocus = false,
  hint,
  kind,
  projectId,
  sessionId,
  onUploaded,
  onSend,
  onStop,
  onCommandResult,
  variant = 'session',
  seed,
  permission,
  empty = false,
}: Props) {
  const prefs = useRef(loadPrefs())
  const [mode, setMode] = useState<ResearchMode>(prefs.current.mode)
  const [localPermission, setLocalPermission] = useState<PermissionPreset>(
    prefs.current.permission,
  )
  const [menu, setMenu] = useState<'slash' | 'mention' | 'mode' | 'permission' | null>(null)
  const [confirming, setConfirming] = useState<PermissionPreset | null>(null)
  const appliedFor = useRef<string | null>(null)
  const currentPermission = permission ?? localPermission
  const [draft, setDraft] = useState('')
  const textarea = useRef<HTMLTextAreaElement>(null)
  const fileInput = useRef<HTMLInputElement>(null)
  const [models, setModels] = useState<ModelProfile[]>(() => listResearchModels())
  const [model, setModel] = useState<ModelProfile | null>(() => selectedResearchModel())
  const [attachments, setAttachments] = useState<ProjectFile[]>([])
  const [mentions, setMentions] = useState<Mention[]>([])
  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState('')
  const [dragging, setDragging] = useState(false)

  const [commands, setCommands] = useState<ResearchCommand[]>([])
  const [commandError, setCommandError] = useState('')
  const [candidates, setCandidates] = useState<Mention[]>([])
  const [active, setActive] = useState(0)
  const [touch] = useState(
    () =>
      typeof window !== 'undefined' &&
      Boolean(window.matchMedia?.('(pointer: coarse)').matches),
  )

  useEffect(() => {
    if (autoFocus) textarea.current?.focus()
  }, [autoFocus])

  useEffect(() => {
    if (!seed) return
    setDraft(seed.text)
    requestAnimationFrame(() => {
      const input = textarea.current
      if (!input) return
      input.focus()
      input.setSelectionRange(seed.text.length, seed.text.length)
    })
  }, [seed?.key, seed?.text])

  useEffect(() => {
    setModels(listResearchModels())
    setModel(selectedResearchModel())
    void syncResearchModel()
  }, [])

  useEffect(() => {
    if (!sessionId) {
      setCommands([])
      return
    }
    let alive = true
    void researchApi
      .listCommands(sessionId)
      .then((items) => {
        if (alive) setCommands(items)
      })
      .catch(() => {
        if (alive) setCommands([])
      })
    return () => {
      alive = false
    }
  }, [sessionId])

  const chooseModel = (next: ModelProfile) => {
    setResearchModel(next)
    setModel(next)
    tourBus.emit('composer:model-changed', { model: next.label })
  }

  const changeMode = (next: ResearchMode) => {
    setMode(next)
    savePrefs(next, localPermission)
    setMenu(null)
  }

  const changePermission = async (next: PermissionPreset) => {
    setMenu(null)
    setConfirming(null)
    setLocalPermission(next)
    prefs.current.permission = next
    savePrefs(mode, next)
    if (!sessionId) return
    try {
      const result = await researchApi.runCommand(sessionId, `/permission ${next}`)
      if (result.event) onCommandResult?.(result.event)
    } catch (error) {
      setCommandError(error instanceof Error ? error.message : '切换权限失败')
    }
  }

  useEffect(() => {
    if (!sessionId || !empty || appliedFor.current === sessionId) return
    appliedFor.current = sessionId
    if (permission) return
    const pref = prefs.current.permission
    if (pref === 'workspace-write') return
    void researchApi
      .runCommand(sessionId, `/permission ${pref}`)
      .then((result) => {
        if (result.event) onCommandResult?.(result.event)
      })
      .catch(() => {
        // 空会话下发偏好失败不打扰用户，发送后仍可手动切换
      })
  }, [sessionId, empty, permission, onCommandResult])

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

  const loadMentions = async (query: string) => {
    const items: Mention[] = []
    if (projectId) {
      try {
        const files = await researchApi.listFiles(projectId)
        for (const file of files) {
          if (file.kind === 'folder') continue
          items.push({
            key: `file-${file.id}`,
            kind: 'file',
            name: file.name,
            path: file.path,
          })
        }
      } catch {
        // 文件树不可用时仍可引用文献库
      }
    }
    try {
      const res = await fetch('/api/papers')
      const data = (await res.json()) as {
        papers?: { id: string; title?: string; filename?: string }[]
      }
      for (const paper of data.papers ?? []) {
        items.push({
          key: `paper-${paper.id}`,
          kind: 'paper',
          name: paper.title || paper.filename || paper.id,
          paperId: paper.id,
        })
      }
    } catch {
      // 文献库读取失败不阻塞文件引用
    }
    const q = query.trim().toLowerCase()
    const filtered = q
      ? items.filter((item) => item.name.toLowerCase().includes(q))
      : items
    setCandidates(filtered.slice(0, 8))
    setActive(0)
  }

  const openMenu = (next: 'slash' | 'mention', query: string) => {
    setMenu(next)
    setCommandError('')
    if (next === 'mention') void loadMentions(query)
    setActive(0)
  }

  const insertMention = (item: Mention) => {
    const token = `@${item.name}`
    setDraft((prev) => prev.replace(/@[^\s@]*$/, `${token} `))
    setMentions((prev) =>
      prev.some((one) => one.key === item.key) ? prev : [...prev, item],
    )
    setMenu(null)
    textarea.current?.focus()
  }

  const options = (): { key: string; label: string; detail: string }[] => {
    if (menu === 'slash') {
      return commands.map((command) => ({
        key: command.name,
        label: `/${command.name}`,
        detail: command.description,
      }))
    }
    return candidates.map((item) => ({
      key: item.key,
      label: item.name,
      detail: item.kind === 'paper' ? '文献库论文' : item.path || '项目文件',
    }))
  }

  const runCommand = async (line: string) => {
    if (!sessionId) return
    setCommandError('')
    try {
      const result = await researchApi.runCommand(sessionId, line)
      setDraft('')
      setMenu(null)
      if (result.event) onCommandResult?.(result.event)
      tourBus.emit('composer:command-run', { line })
    } catch (error) {
      setCommandError(error instanceof Error ? error.message : '命令执行失败')
    }
  }

  const pickOption = (index: number) => {
    const list = options()
    const option = list[index]
    if (!option) return
    if (menu === 'slash') {
      const command = commands.find((item) => item.name === option.key)
      if (!command) return
      if (command.input?.hint) {
        setDraft(`/${command.name} `)
        setMenu(null)
        textarea.current?.focus()
      } else {
        void runCommand(`/${command.name}`)
      }
      return
    }
    const item = candidates.find((one) => one.key === option.key)
    if (item) insertMention(item)
  }

  const submit = () => {
    const text = draft.trim()
    if (!text) return
    const extras: string[] = []
    if (mentions.length > 0) {
      extras.push(
        `引用对象（用户指定，优先读取）：\n${mentions
          .map((item) =>
            item.kind === 'paper'
              ? `- ${item.name}（论文 ${item.paperId}）`
              : `- ${item.name}（路径：${item.path}）`,
          )
          .join('\n')}`,
      )
    }
    if (attachments.length > 0) {
      extras.push(
        `附件（已保存在项目目录，可直接读取）：\n${attachments
          .map((item) => `- ${item.path}`)
          .join('\n')}`,
      )
    }
    const body = extras.length ? `${text}\n\n${extras.join('\n\n')}` : text
    setDraft('')
    setAttachments([])
    setMentions([])
    setUploadError('')
    setCommandError('')
    tourBus.emit('composer:message-sent', { text })
    const scope: WorkspaceScope =
      currentPermission === 'read-only' ? 'readonly' : 'workspace'
    onSend(body, { mode, scope })
  }

  const currentCommand = draft.trim().startsWith('/')
    ? draft.trim().slice(1).split(/\s+/)[0]
    : ''

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.nativeEvent.isComposing) return
    const list = menu ? options() : []
    if (menu && menu !== 'mode' && menu !== 'permission') {
      if (event.key === 'ArrowDown') {
        event.preventDefault()
        setActive((prev) => (list.length ? (prev + 1) % list.length : 0))
        return
      }
      if (event.key === 'ArrowUp') {
        event.preventDefault()
        setActive((prev) => (list.length ? (prev - 1 + list.length) % list.length : 0))
        return
      }
      if (event.key === 'Escape') {
        event.preventDefault()
        setMenu(null)
        return
      }
      if (event.key === 'Enter') {
        event.preventDefault()
        pickOption(active)
        return
      }
    }
    if (event.key === 'Enter' && !event.shiftKey && !touch) {
      event.preventDefault()
      const slash = commands.find((item) => item.name === currentCommand)
      if (slash && draft.trim() === `/${slash.name}` && sessionId) {
        void runCommand(draft.trim())
        return
      }
      submit()
    }
  }

  const onChange = (value: string) => {
    setDraft(value)
    const tail = value.slice(0, value.length)
    if (/@[^\s@]*$/.test(tail)) {
      openMenu('mention', (tail.match(/@([^\s@]*)$/)?.[1] ?? '').trim())
      return
    }
    if (/(^|\s)\/[a-z0-9_-]*$/.test(tail)) {
      openMenu('slash', '')
      return
    }
    if (menu === 'mention' || menu === 'slash') setMenu(null)
  }

  const hintText =
    hint ??
    (running
      ? '正在研究，可随时停止'
      : touch
        ? '点发送开始，回车换行'
        : 'Enter 发送 · Shift+Enter 换行 · / 指令 · @ 引用')

  return (
    <div className={`rc${variant === 'home' ? ' rc-home' : ''}`} data-tour="composer">
      <div
        className={`rc-box${dragging ? ' is-dragging' : ''}`}
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
        {(attachments.length > 0 || mentions.length > 0) && (
          <div className="rc-chips">
            {mentions.map((item) => (
              <span className="rc-chip" key={item.key} title={item.path || item.paperId}>
                <IconAt />
                <span className="rc-chip-name">{item.name}</span>
                <button
                  type="button"
                  className="rc-chip-remove"
                  title="移除引用"
                  onClick={() => {
                    setMentions((prev) => prev.filter((one) => one.key !== item.key))
                    setDraft((prev) =>
                      prev.replace(`@${item.name} `, '').replace(`@${item.name}`, ''),
                    )
                  }}
                >
                  <IconClose />
                </button>
              </span>
            ))}
            {attachments.map((item) => (
              <span className="rc-chip" key={item.id} title={item.path}>
                <span className="rc-chip-name">{item.name}</span>
                <span className="rc-chip-size">{sizeText(item.size)}</span>
                <button
                  type="button"
                  className="rc-chip-remove"
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
        {(uploading || uploadError || commandError) && (
          <div className={`rc-note${uploadError || commandError ? ' is-error' : ''}`}>
            {commandError || uploadError || '正在上传附件…'}
          </div>
        )}

        <textarea
          ref={textarea}
          value={draft}
          placeholder={
            placeholder ??
            (kind === 'review'
              ? '描述综述主题与目标读者，/ 调用指令，@ 引用文件或论文'
              : '描述你想研究的问题，/ 调用指令，@ 引用文件或论文')
          }
          rows={2}
          onChange={(event) => onChange(event.target.value)}
          onPaste={(event) => {
            const files = Array.from(event.clipboardData.files)
            if (files.length > 0 && projectId) {
              event.preventDefault()
              void addFiles(files)
            }
          }}
          onKeyDown={onKeyDown}
        />

        {menu && menu !== 'mode' && (
          <div className="rc-menu rc-suggest" role="listbox">
            {options().length === 0 ? (
              <p className="rc-empty">
                {menu === 'slash'
                  ? sessionId
                    ? '没有可用指令'
                    : '发送首条消息后可使用 / 指令'
                  : '没有可引用的文件或论文'}
              </p>
            ) : (
              options().map((option, index) => (
                <button
                  type="button"
                  key={option.key}
                  className={`rc-option${index === active ? ' is-active' : ''}`}
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => pickOption(index)}
                >
                  {menu === 'slash' ? <IconSlash /> : <IconAt />}
                  <span className="rc-option-label">{option.label}</span>
                  <span className="rc-option-detail">{option.detail}</span>
                </button>
              ))
            )}
          </div>
        )}

        <div className="rc-foot">
          <div className="rc-tools">
            <div className="rc-mode-wrap">
              <button
                type="button"
                className="rc-mode"
                title="切换研究模式"
                onClick={() => setMenu(menu === 'mode' ? null : 'mode')}
              >
                <IconShield />
                <span>{mode === 'deep' ? '深度研究' : '标准模式'}</span>
                <IconChevronDown />
              </button>
              {menu === 'mode' && (
                <div className="rc-menu rc-mode-menu is-up">
                  <button
                    type="button"
                    className={`rc-option${mode === 'standard' ? ' is-active' : ''}`}
                    onClick={() => changeMode('standard')}
                  >
                    <span className="rc-option-label">标准模式</span>
                    <span className="rc-option-detail">直接给结论，按需检索</span>
                  </button>
                  <button
                    type="button"
                    className={`rc-option${mode === 'deep' ? ' is-active' : ''}`}
                    onClick={() => changeMode('deep')}
                  >
                    <span className="rc-option-label">深度研究</span>
                    <span className="rc-option-detail">多轮检索与交叉核验，列明来源</span>
                  </button>
                </div>
              )}
            </div>
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
                  className="rc-icon"
                  title="添加附件"
                  disabled={uploading}
                  onClick={() => fileInput.current?.click()}
                >
                  <IconPlus />
                </button>
              </>
            )}
            <div className="rc-mode-wrap">
              <button
                type="button"
                className={`rc-mode${currentPermission === 'danger-full-access' ? ' is-danger' : ''}`}
                title="切换终端与工作区权限"
                onClick={() => {
                  setConfirming(null)
                  setMenu(menu === 'permission' ? null : 'permission')
                }}
              >
                <IconShield />
                <span>{PERMISSION_LABELS[currentPermission]}</span>
                <IconChevronDown />
              </button>
              {menu === 'permission' && (
                <div className="rc-menu rc-mode-menu is-up">
                  <button
                    type="button"
                    className={`rc-option${currentPermission === 'read-only' ? ' is-active' : ''}`}
                    onClick={() => void changePermission('read-only')}
                  >
                    <span className="rc-option-label">只读分析</span>
                    <span className="rc-option-detail">可运行命令，但不能写项目文件</span>
                  </button>
                  <button
                    type="button"
                    className={`rc-option${currentPermission === 'workspace-write' ? ' is-active' : ''}`}
                    onClick={() => void changePermission('workspace-write')}
                  >
                    <span className="rc-option-label">沙箱执行</span>
                    <span className="rc-option-detail">仅项目文件夹可写，越界时向你申请</span>
                  </button>
                  {confirming === 'danger-full-access' ? (
                    <div className="rc-risk">
                      <p>完全权限会关闭沙箱，命令可读写项目文件夹之外的位置。</p>
                      <div className="rc-risk-actions">
                        <button type="button" onClick={() => setConfirming(null)}>
                          取消
                        </button>
                        <button
                          type="button"
                          className="is-primary"
                          onClick={() => void changePermission('danger-full-access')}
                        >
                          确认启用
                        </button>
                      </div>
                    </div>
                  ) : (
                    <button
                      type="button"
                      className={`rc-option${currentPermission === 'danger-full-access' ? ' is-active' : ''}`}
                      onClick={() => setConfirming('danger-full-access')}
                    >
                      <span className="rc-option-label">完全权限</span>
                      <span className="rc-option-detail">关闭沙箱，可读写任意位置</span>
                    </button>
                  )}
                </div>
              )}
            </div>
            <button
              type="button"
              className="rc-icon"
              title="引用文件或论文（@）"
              onClick={() => {
                openMenu('mention', '')
                textarea.current?.focus()
              }}
            >
              <IconAt />
            </button>
            <button
              type="button"
              className="rc-icon"
              title="调用指令（/）"
              onClick={() => {
                openMenu('slash', '')
                textarea.current?.focus()
              }}
            >
              <IconSlash />
            </button>
          </div>
          <div className="rc-right">
            {hintText && <span className="rc-hint">{hintText}</span>}
            <ModelPicker models={models} current={model} onChoose={chooseModel} />
            {running ? (
              <button className="rc-send is-stop" title="停止" onClick={onStop}>
                <IconStop />
              </button>
            ) : (
              <button
                className="rc-send"
                title="发送"
                disabled={!draft.trim()}
                onClick={submit}
              >
                <IconArrowUp />
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}