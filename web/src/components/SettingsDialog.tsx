import { useCallback, useEffect, useRef, useState } from 'react'
import {
  canInstallInApp,
  checkUpdate,
  formatCheckTime,
  installUpdateInApp,
  isDesktopApp,
  loadUpdateCache,
  parseUpdateNotes,
  UPDATE_DAY_MS,
  type UpdateInfo,
} from '../lib/update'
import {
  activeModel,
  BASE_URL_OPTIONS,
  isModelReady,
  KIND_HINT,
  KIND_LABEL,
  loadModelSettings,
  maskKey,
  newModelId,
  normalizeBaseUrl,
  presetById,
  presetModel,
  presetsFor,
  PRESETS,
  providerName,
  saveModelSettings,
  toPayload,
  type ModelKind,
  type ModelProfile,
  type ModelSettings,
  type ProviderModel,
  type ProviderPreset,
} from '../lib/settings'
import { Katex } from './Katex'
import { ReviewTemplatePanel } from './ReviewTemplatePanel'
import { ToolchainPanel } from './ToolchainPanel'
import { MobileServerPanel } from './MobileSetup'
import { isMobileShell } from '../lib/mobile'
import { loadScholarSettings, saveScholarSettings } from '../lib/scholarConfig'
import {
  getOnboarding,
  postOnboarding,
  type OnboardingState,
} from '../lib/onboarding'
import { tourBus } from '../tour/tourBus'
import {
  IconBack,
  IconBook,
  IconChat,
  IconCheck,
  IconDownload,
  IconPlus,
  IconRefresh,
  IconSearch,
  IconSettings,
  IconSpark,
} from './icons'
import {
  TRANSLATION_LANGS,
  loadTargetLang,
  saveTargetLang,
} from '../lib/translation'
import {
  loadAutoHighlights,
  saveAutoHighlights,
} from '../lib/readingPrefs'

interface Props {
  open: boolean
  onClose: () => void
  /** 阅读页当前论文里的一张公式裁图，用来测试识图模型 */
  sampleImage?: string
  onSaved?: (settings: ModelSettings) => void
  initialKind?: ModelKind
}

type TestState =
  | { kind: 'idle' }
  | { kind: 'testing' }
  | { kind: 'ok'; ms: number; latex?: string; vision: boolean; fallback: boolean }
  | { kind: 'error'; message: string }

interface EditorState {
  mode: 'preset' | 'custom' | 'edit'
  kind: ModelKind
  id?: string
  preset?: ProviderPreset
  presetModel?: ProviderModel
  label?: string
  baseUrl: string
  model: string
  apiKey: string
  provider: string
  keyUrl?: string
}

const FALLBACK_FORMULA = '/demo/assets/equations/eq_0033.png'

async function blobToBase64(blob: Blob): Promise<string> {
  const buf = await blob.arrayBuffer()
  let binary = ''
  const bytes = new Uint8Array(buf)
  for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i])
  return btoa(binary)
}

async function fetchImageBase64(url: string): Promise<string> {
  const res = await fetch(url)
  if (!res.ok) throw new Error(`示例图加载失败（HTTP ${res.status}）`)
  return blobToBase64(await res.blob())
}

async function postJson(url: string, body: unknown): Promise<any> {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`)
  return data
}

function draftProfile(editor: EditorState): ModelProfile {
  return {
    id: editor.id ?? 'draft',
    kind: editor.kind,
    label: editor.label || editor.model || 'draft',
    baseUrl: normalizeBaseUrl(editor.baseUrl),
    model: editor.model.trim(),
    apiKey: editor.apiKey.trim(),
    provider: editor.provider,
    keyUrl: editor.keyUrl,
    createdAt: 0,
    updatedAt: 0,
  }
}

function providerLabel(editor: EditorState): string {
  const preset =
    presetById(editor.provider) ??
    PRESETS.find(
      (p) => p.baseUrl === normalizeBaseUrl(editor.baseUrl),
    )
  return preset?.provider ?? '服务商官网'
}

function TestBox({ test }: { test: TestState }) {
  if (test.kind === 'idle') {
    return (
      <div className="test-box" data-state="idle">
        填好后可以点「测试连接」验证地址、密钥和模型名是否匹配。
      </div>
    )
  }
  if (test.kind === 'testing') {
    return (
      <div className="test-box" data-state="idle">
        正在连接…
      </div>
    )
  }
  if (test.kind === 'error') {
    return (
      <div className="test-box" data-state="error">
        <div className="test-err">
          失败：{test.message}
          <br />
          常见原因：本地服务没启动 / 地址末尾多了或少了 <code>/v1</code> / 模型名不对 / Key 无效 /
          思考模型把 max_tokens 用完了（重试或换一个模型）。
        </div>
      </div>
    )
  }
  return (
    <div className="test-box" data-state="ok">
      <div className="test-ok">
        <IconCheck /> {test.vision && !test.fallback ? '识图正常' : '连接正常'} · {test.ms}ms
      </div>
      {test.vision && test.fallback && (
        <div className="test-warn">没有取到示例公式图，只验证了连通性。</div>
      )}
      {test.latex ? (
        <div className="test-latex">
          <Katex tex={test.latex} />
          <code>{test.latex}</code>
        </div>
      ) : test.vision && !test.fallback ? (
        <div className="test-warn">
          连通性正常，但示例公式没识别出结果（可能是模型不支持图像输入）。
        </div>
      ) : null}
    </div>
  )
}

export function SettingsDialog({
  open,
  onClose,
  sampleImage,
  onSaved,
  initialKind = 'answer',
}: Props) {
  const [settings, setSettings] = useState<ModelSettings>(loadModelSettings)
  const [kind, setKind] = useState<ModelKind>(initialKind)
  const [tab, setTab] = useState<
    | 'models'
    | 'translation'
    | 'reading'
    | 'scholar'
    | 'tools'
    | 'review'
    | 'tour'
    | 'update'
    | 'server'
  >('models')
  const [editor, setEditor] = useState<EditorState | null>(null)
  const [showKey, setShowKey] = useState(false)
  const [test, setTest] = useState<TestState>({ kind: 'idle' })
  const [selections, setSelections] = useState<Record<string, string>>({})
  const [updateInfo, setUpdateInfo] = useState<UpdateInfo | null>(
    () => loadUpdateCache()?.data ?? null,
  )
  const [updateCheckedAt, setUpdateCheckedAt] = useState(
    () => loadUpdateCache()?.checkedAt ?? 0,
  )
  const [updateChecking, setUpdateChecking] = useState(false)
  const [updateError, setUpdateError] = useState('')
  const [installing, setInstalling] = useState(false)
  const [installError, setInstallError] = useState('')
  const [closing, setClosing] = useState(false)
  const [tourVisible, setTourVisible] = useState(false)
  const bodyRef = useRef<HTMLDivElement>(null)
  const closeTimer = useRef<number | null>(null)
  const desktop = isDesktopApp()
  const mobile = isMobileShell()

  // 安卓端没有研究与综述，误入这些分类时回到配置模型
  useEffect(() => {
    if (mobile && (tab === 'tools' || tab === 'review' || tab === 'tour' || tab === 'update')) {
      setTab('models')
    }
  }, [mobile, tab])

  // 整页滑出需要先播完退场动画，再真正卸载；重复调用只保留一个定时器。
  const requestClose = useCallback(() => {
    if (closeTimer.current != null) return
    setClosing(true)
    closeTimer.current = window.setTimeout(() => {
      closeTimer.current = null
      setClosing(false)
      onClose()
    }, 210)
  }, [onClose])

  useEffect(() => {
    if (!open || !desktop) {
      setTourVisible(false)
      return
    }
    let alive = true
    void getOnboarding()
      .then((state) => {
        if (alive) setTourVisible(!state.dismissed)
      })
      .catch(() => {
        if (alive) setTourVisible(false)
      })
    return () => {
      alive = false
    }
  }, [desktop, open])

  const hideTour = useCallback(() => {
    setTourVisible(false)
    setTab('models')
  }, [])

  useEffect(() => {
    if (!open) return
    setClosing(false)
    if (closeTimer.current != null) {
      window.clearTimeout(closeTimer.current)
      closeTimer.current = null
    }
  }, [open])

  useEffect(
    () => () => {
      if (closeTimer.current != null) window.clearTimeout(closeTimer.current)
    },
    [],
  )

  useEffect(() => {
    bodyRef.current?.scrollTo({ top: 0 })
  }, [open, tab, kind, editor?.mode, editor?.id])

  useEffect(() => {
    if (!open) return
    setSettings(loadModelSettings())
    setKind(initialKind)
    setTab('models')
    setEditor(null)
    setShowKey(false)
    setTest({ kind: 'idle' })
    setSelections({})
    setUpdateError('')
    setInstalling(false)
    setInstallError('')
  }, [open, initialKind])

  const runUpdateCheck = useCallback(async (force = false) => {
    setUpdateChecking(true)
    setUpdateError('')
    try {
      const data = await checkUpdate(force)
      setUpdateInfo(data)
      setUpdateCheckedAt(Number(data.checked_at) || Date.now())
      return data
    } catch (e) {
      setUpdateError(e instanceof Error ? e.message : '检查更新失败')
      return null
    } finally {
      setUpdateChecking(false)
    }
  }, [])

  // 打开设置时：本地缓存超过一天就自动检查一次，手动检查不受这个限制
  useEffect(() => {
    if (!open) return
    const cached = loadUpdateCache()
    if (cached) {
      setUpdateInfo(cached.data)
      setUpdateCheckedAt(cached.checkedAt)
    }
    if (!cached || Date.now() - cached.checkedAt >= UPDATE_DAY_MS) {
      void runUpdateCheck(false)
    }
  }, [open, runUpdateCheck])

  const installUpdate = async () => {
    setInstalling(true)
    setInstallError('')
    try {
      await installUpdateInApp()
    } catch (e) {
      setInstallError(e instanceof Error ? e.message : '自动更新失败，请下载安装包')
    } finally {
      setInstalling(false)
    }
  }

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      if (editor) {
        setEditor(null)
        setTest({ kind: 'idle' })
      } else {
        requestClose()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, editor, requestClose])

  if (!open) return null

  const formulaSample = sampleImage || FALLBACK_FORMULA
  const current = activeModel(settings, kind)
  const list = settings.models.filter((m) => m.kind === kind)
  const presets = presetsFor(kind)

  const savedForProvider = (preset: ProviderPreset): ModelProfile | undefined => {
    const saved = list.filter(
      (m) =>
        m.baseUrl === normalizeBaseUrl(preset.baseUrl) &&
        preset.models.some((pm) => pm.id === m.model),
    )
    return saved.find((m) => m.id === settings.active[kind]) ?? saved[0]
  }

  const selectedForProvider = (preset: ProviderPreset): ProviderModel => {
    const selectedId =
      selections[preset.id] ??
      savedForProvider(preset)?.model ??
      preset.defaultModel
    return presetModel(preset, selectedId)
  }

  const persist = (next: ModelSettings) => {
    saveModelSettings(next)
    setSettings(next)
    onSaved?.(next)
    if (next.models.some((model) => model.kind === 'answer' && isModelReady(model))) {
      tourBus.emit('settings:saved')
    }
  }

  const switchKind = (nextKind: ModelKind) => {
    setKind(nextKind)
    setEditor(null)
    setShowKey(false)
    setTest({ kind: 'idle' })
    setSelections({})
  }

  const openPreset = (preset: ProviderPreset, model: ProviderModel) => {
    const existing = settings.models.find(
      (m) =>
        m.kind === kind &&
        m.baseUrl === normalizeBaseUrl(preset.baseUrl) &&
        m.model === model.id,
    )
    setEditor({
      mode: 'preset',
      kind,
      id: existing?.id,
      preset,
      presetModel: model,
      label: existing?.label ?? model.label,
      baseUrl: preset.baseUrl,
      model: model.id,
      apiKey: existing?.apiKey ?? '',
      provider: preset.id,
      keyUrl: preset.keyUrl,
    })
    setShowKey(false)
    setTest({ kind: 'idle' })
  }

  const openCustom = () => {
    setEditor({
      mode: 'custom',
      kind,
      baseUrl: '',
      model: '',
      apiKey: '',
      provider: 'custom',
    })
    setShowKey(false)
    setTest({ kind: 'idle' })
  }

  const openEdit = (model: ModelProfile) => {
    setEditor({
      mode: 'edit',
      kind,
      id: model.id,
      label: model.label,
      baseUrl: model.baseUrl,
      model: model.model,
      apiKey: model.apiKey,
      provider: model.provider,
      keyUrl: model.keyUrl,
    })
    setShowKey(false)
    setTest({ kind: 'idle' })
  }

  const closeEditor = () => {
    setEditor(null)
    setShowKey(false)
    setTest({ kind: 'idle' })
  }

  const patchEditor = (patch: Partial<EditorState>) => {
    setEditor((prev) => (prev ? { ...prev, ...patch } : prev))
  }

  const draftReady = editor ? isModelReady(draftProfile(editor)) : false

  const saveEditor = () => {
    if (!editor) return
    const draft = draftProfile(editor)
    if (!isModelReady(draft)) return

    const now = Date.now()
    const models = [...settings.models]
    let id = editor.id ?? ''
    // 自定义模型如果恰好是推荐服务商里的同一个模型，自动套用官方名称和取 Key 链接
    const matchedProvider = PRESETS.find(
      (p) =>
        p.kind === editor.kind &&
        p.baseUrl === draft.baseUrl &&
        p.models.some((m) => m.id === draft.model),
    )
    const matchedModel = matchedProvider?.models.find(
      (m) => m.id === draft.model,
    )

    if (editor.mode === 'preset' && editor.preset) {
      const existing = models.find(
        (m) =>
          m.kind === editor.kind &&
          m.baseUrl === draft.baseUrl &&
          m.model === draft.model,
      )
      if (existing) {
        id = existing.id
        models[models.indexOf(existing)] = {
          ...existing,
          apiKey: draft.apiKey,
          label: editor.presetModel?.label ?? draft.model,
          provider: editor.preset.id,
          keyUrl: editor.preset.keyUrl,
          updatedAt: now,
        }
      } else {
        id = newModelId()
        models.push({
          ...draft,
          id,
          label: editor.presetModel?.label ?? draft.model,
          provider: editor.preset.id,
          keyUrl: editor.preset.keyUrl,
          createdAt: now,
          updatedAt: now,
        })
      }
    } else if (editor.id) {
      const existing = models.find((m) => m.id === editor.id)
      if (existing) {
        // 名称跟模型名一致时，改模型名就顺带更新展示名
        const label = matchedModel
          ? matchedModel.label
          : existing.label === existing.model
            ? draft.model
            : existing.label
        models[models.indexOf(existing)] = {
          ...existing,
          ...draft,
          id: existing.id,
          label,
          provider: matchedProvider?.id ?? existing.provider,
          keyUrl: matchedProvider?.keyUrl ?? existing.keyUrl,
          updatedAt: now,
        }
        id = existing.id
      } else {
        // 编辑过程中那条配置被别处删掉了：按新增处理，避免 active 指向空 id
        id = newModelId()
        models.push({
          ...draft,
          id,
          label: matchedModel?.label ?? draft.model,
          provider: matchedProvider?.id ?? 'custom',
          keyUrl: matchedProvider?.keyUrl,
          createdAt: now,
          updatedAt: now,
        })
      }
    } else {
      const existing = models.find(
        (m) =>
          m.kind === editor.kind &&
          m.baseUrl === draft.baseUrl &&
          m.model === draft.model,
      )
      const label = matchedModel?.label ?? draft.model
      const provider = matchedProvider?.id ?? 'custom'
      const keyUrl = matchedProvider?.keyUrl
      if (existing) {
        id = existing.id
        models[models.indexOf(existing)] = {
          ...existing,
          ...draft,
          id: existing.id,
          label,
          provider,
          keyUrl,
          updatedAt: now,
        }
      } else {
        id = newModelId()
        models.push({
          ...draft,
          id,
          label,
          provider,
          keyUrl,
          createdAt: now,
          updatedAt: now,
        })
      }
    }

    persist({
      version: 2,
      models,
      active: { ...settings.active, [editor.kind]: id },
    })
    // 「保存并使用」是完成动作：保存成功就退出设置，不把用户留在弹窗里。
    // 需要再配另一类模型时，从顶栏齿轮重新打开即可。
    closeEditor()
    requestClose()
  }

  const selectModel = (model: ModelProfile) => {
    if (!isModelReady(model)) {
      openEdit(model)
      return
    }
    setTest({ kind: 'idle' })
    persist({
      version: 2,
      models: settings.models,
      active: { ...settings.active, [kind]: model.id },
    })
  }

  const removeModel = (model: ModelProfile) => {
    if (!window.confirm(`删除「${model.label}」？保存在本机的这份 Key 也会一起删除。`)) {
      return
    }
    const models = settings.models.filter((m) => m.id !== model.id)
    const active = { ...settings.active }
    if (active[model.kind] === model.id) {
      active[model.kind] = models.find((m) => m.kind === model.kind)?.id ?? ''
    }
    setTest({ kind: 'idle' })
    persist({ version: 2, models, active })
    if (editor?.id === model.id) closeEditor()
  }

  const testModel = async (model: ModelProfile) => {
    if (!isModelReady(model)) return
    setTest({ kind: 'testing' })
    try {
      const config = toPayload(model)
      if (model.kind === 'vision') {
        let image = ''
        try {
          image = await fetchImageBase64(formulaSample)
        } catch {
          image = ''
        }
        if (image) {
          const data = await postJson('/api/formula', {
            config,
            image_base64: image,
          })
          setTest({
            kind: 'ok',
            ms: Number(data.ms) || 0,
            latex: String(data.latex || ''),
            vision: true,
            fallback: false,
          })
          return
        }
      }
      const data = await postJson('/api/ping', { config })
      setTest({
        kind: 'ok',
        ms: Number(data.ms) || 0,
        vision: model.kind === 'vision',
        fallback: model.kind === 'vision',
      })
    } catch (e) {
      setTest({
        kind: 'error',
        message: e instanceof Error ? e.message : String(e),
      })
    }
  }

  const pageTitle =
    tab === 'models'
      ? '配置模型'
      : tab === 'translation'
        ? '译文语言'
        : tab === 'reading'
          ? '自动阅读'
          : tab === 'scholar'
            ? '文献检索'
            : tab === 'tools'
              ? '写作工具'
              : tab === 'review'
                ? '综述模板'
                : tab === 'tour'
                  ? '新手教程'
                  : tab === 'server'
                    ? '服务器连接'
                    : '检查版本更新'
  const pageHint =
    tab === 'models'
      ? '回答模型负责问答与翻译，识图模型负责论文里的图片识别；两者可以分开配置，修改会自动保存在本机。'
      : tab === 'translation'
        ? '设置阅读页把英文论文翻译成哪种语言。切换后，已翻译的论文会按新语言重新翻译；原文、标注和笔记不受影响。'
        : tab === 'reading'
          ? '控制阅读时是否自动提取研究目标、研究方法和主要结论。关闭后不再发起额外的模型调用，已经生成的重点仍会保留。'
          : tab === 'scholar'
        ? '这里配置 Semantic Scholar 与 CORE 的检索密钥；密钥只保存在本机，不会上传到服务器。'
        : tab === 'tools'
          ? '导出 PDF 需要本机的 LaTeX 引擎；这里可以检测并快速安装。只检测本机工具，不会上传任何文件。'
          : tab === 'review'
            ? '设定写综述时用的章节骨架：可以用系统默认、上传自己的模板，或从文献库选一篇综述。模板不会上传文献或密钥。'
            : tab === 'tour'
              ? '只在桌面客户端首次启动时自动出现一次。之后可以在这里重新观看或继续上次的进度。'
              : tab === 'server'
                ? '手机端通过这里连接运行 Paperead 后端的电脑或服务器。地址和口令只保存在本机。'
                : '应用每天自动检查一次新版本，也可以随时手动检查。'

  return (
    <div
      className={`settings-page${closing ? ' is-closing' : ''}`}
      role="dialog"
      aria-modal
      aria-label="设置"
    >
      <div className="settings-page-shell">
        <aside className="settings-sidebar">
          <div className="library-brand">
            <button
              className="icon-btn settings-mobile-back"
              onClick={requestClose}
              title="返回主界面"
              aria-label="返回主界面"
            >
              <IconBack />
            </button>
            <span className="brand-name">设置</span>
          </div>
          <nav className="library-nav" aria-label="设置分类">
            <button
              className="library-nav-item"
              data-active={tab === 'models'}
              onClick={() => setTab('models')}
            >
              <IconSettings />
              <span>配置模型</span>
            </button>
            <button
              className="library-nav-item"
              data-active={tab === 'translation'}
              onClick={() => setTab('translation')}
            >
              <IconChat />
              <span>译文语言</span>
            </button>
            <button
              className="library-nav-item"
              data-active={tab === 'reading'}
              onClick={() => setTab('reading')}
            >
              <IconSpark />
              <span>自动阅读</span>
            </button>
            <button
              className="library-nav-item"
              data-active={tab === 'scholar'}
              onClick={() => setTab('scholar')}
            >
              <IconSearch />
              <span>文献检索</span>
            </button>
            {mobile && (
              <button
                className="library-nav-item"
                data-active={tab === 'server'}
                onClick={() => setTab('server')}
              >
                <IconRefresh />
                <span>服务器连接</span>
              </button>
            )}
            {!mobile && (
              <>
                <button
                  className="library-nav-item"
                  data-active={tab === 'tools'}
                  onClick={() => setTab('tools')}
                >
                  <IconDownload />
                  <span>写作工具</span>
                </button>
                <button
                  className="library-nav-item"
                  data-active={tab === 'review'}
                  onClick={() => setTab('review')}
                >
                  <IconBook />
                  <span>综述模板</span>
                </button>
              </>
            )}
            {desktop && tourVisible && (
              <button
                className="library-nav-item"
                data-active={tab === 'tour'}
                onClick={() => setTab('tour')}
              >
                <IconSpark />
                <span>新手教程</span>
              </button>
            )}
            {!mobile && (
              <button
                className="library-nav-item"
                data-active={tab === 'update'}
                onClick={() => setTab('update')}
              >
                <IconRefresh />
                <span>检查版本更新</span>
                {updateInfo?.has_update && (
                  <i className="settings-nav-dot" title="发现新版本" />
                )}
              </button>
            )}
          </nav>
          <div className="settings-sidebar-foot">
            <button className="library-nav-item" onClick={requestClose}>
              <IconBack />
              <span>返回主界面</span>
            </button>
            <span className="settings-nav-version">v{updateInfo?.current || '—'}</span>
          </div>
        </aside>

        <main className="settings-main">
          <header className="settings-main-head">
            <div>
              <h1>{pageTitle}</h1>
              <p>{pageHint}</p>
            </div>
            <button
              className="icon-btn settings-desktop-back"
              onClick={requestClose}
              title="返回主界面"
              aria-label="返回主界面"
            >
              <IconBack />
            </button>
          </header>
            {tab === 'models' ? (
              <>
                <div className="model-tabs" role="tablist" aria-label="模型类型">
          {(['answer', 'vision'] as const).map((k) => (
            <button
              key={k}
              className="model-tab"
              role="tab"
              aria-selected={kind === k}
              data-active={kind === k}
              onClick={() => switchKind(k)}
            >
              <b>{KIND_LABEL[k]}</b>
              <span>{k === 'answer' ? '问答 · 翻译 · 导读' : '图片 · 公式识别'}</span>
            </button>
          ))}
        </div>

        <div className="modal-body" ref={bodyRef}>
          {editor ? (
            <div className="model-editor">
              <button className="back-link" onClick={closeEditor}>
                <IconBack /> 返回模型列表
              </button>

              <div className="editor-head">
                <div>
                  <h3>
                    {editor.mode === 'custom'
                      ? `自定义${KIND_LABEL[editor.kind]}`
                      : editor.mode === 'preset'
                        ? `添加 ${editor.preset?.label ?? ''} 模型`
                        : `编辑${KIND_LABEL[editor.kind]}`}
                  </h3>
                  <p>{KIND_HINT[editor.kind]}</p>
                </div>
                {editor.keyUrl && (
                  <a
                    className="btn small key-link"
                    href={editor.keyUrl}
                    target="_blank"
                    rel="noreferrer"
                  >
                    去官网获取 API Key ↗
                  </a>
                )}
              </div>

              {editor.mode === 'preset' && editor.preset ? (
                <div className="preset-summary">
                  <div>
                    <span>服务商</span>
                    <b>{editor.preset.provider}</b>
                  </div>
                  <div>
                    <span>模型</span>
                    <b>{editor.presetModel?.label ?? editor.model}</b>
                  </div>
                  <div>
                    <span>接口地址</span>
                    <code>{editor.baseUrl}</code>
                  </div>
                  <div>
                    <span>模型名</span>
                    <code>{editor.model}</code>
                  </div>
                  <p>只需要粘贴 API Key，其余信息已经填好。</p>
                </div>
              ) : (
                <>
                  <label className="field-label" htmlFor="base">
                    接口地址
                  </label>
                  <input
                    id="base"
                    className="input"
                    value={editor.baseUrl}
                    onChange={(e) => patchEditor({ baseUrl: e.target.value })}
                    placeholder="https://api.deepseek.com/v1"
                    spellCheck={false}
                    autoComplete="off"
                  />
                  <div className="base-chips">
                    {BASE_URL_OPTIONS.map((option) => (
                      <button
                        key={option.url}
                        type="button"
                        className="base-chip"
                        data-active={normalizeBaseUrl(editor.baseUrl) === option.url}
                        onClick={() => patchEditor({ baseUrl: option.url })}
                      >
                        {option.label}
                      </button>
                    ))}
                  </div>

                  <label className="field-label" htmlFor="model">
                    模型名
                  </label>
                  <input
                    id="model"
                    className="input"
                    value={editor.model}
                    onChange={(e) => patchEditor({ model: e.target.value })}
                    placeholder={editor.kind === 'vision' ? 'qwen3-vl-plus' : 'deepseek-flash'}
                    spellCheck={false}
                    autoComplete="off"
                  />
                  <p className="hint">
                    自定义模型只需要填接口地址、模型名和 API Key 三项，其他参数用默认值。
                  </p>
                </>
              )}

              <label className="field-label" htmlFor="key">
                API Key
              </label>
              <div className="input-row">
                <input
                  id="key"
                  className="input"
                  type={showKey ? 'text' : 'password'}
                  value={editor.apiKey}
                  onChange={(e) => patchEditor({ apiKey: e.target.value })}
                  placeholder="粘贴你的 API Key"
                  spellCheck={false}
                  autoComplete="off"
                />
                <button className="ghost-btn" onClick={() => setShowKey((v) => !v)}>
                  {showKey ? '隐藏' : '显示'}
                </button>
              </div>

              {editor.keyUrl ? (
                <p className="hint">
                  还没有 Key？
                  <a href={editor.keyUrl} target="_blank" rel="noreferrer">
                    去 {providerLabel(editor)} 创建 ↗
                  </a>
                  （会在新标签页打开）
                </p>
              ) : (
                <p className="hint">在服务商控制台创建 API Key 后粘贴到这里。</p>
              )}
              <p className="hint">
                密钥只保存在这台机器的 localStorage，请求时随 body 发给本地服务，服务端不落盘。
              </p>

              {editor.kind === 'vision' && (
                <div className="vision-sample">
                  <img src={formulaSample} alt="示例公式" />
                  <span>测试会用这张公式图验证识图能力。</span>
                </div>
              )}

              <TestBox test={test} />
            </div>
          ) : (
            <>
              <div className="model-active">
                <div className="model-active-head">
                  <span>当前使用</span>
                  {current && (
                    <span
                      className="model-state"
                      data-ready={isModelReady(current)}
                    >
                      {isModelReady(current) ? '可用' : '待补全'}
                    </span>
                  )}
                </div>
                {current ? (
                  <div className="model-active-body">
                    <div className="model-active-main">
                      <b>{current.label}</b>
                      <span>
                        {providerName(current)} · <code>{current.model}</code>
                      </span>
                      <span className="model-key">
                        {current.apiKey
                          ? `Key ${maskKey(current.apiKey)}`
                          : '还没有填 API Key'}
                      </span>
                    </div>
                    <div className="model-active-actions">
                      <button
                        className="btn small"
                        onClick={() => testModel(current)}
                        disabled={!isModelReady(current) || test.kind === 'testing'}
                      >
                        测试
                      </button>
                      <button className="btn small" onClick={() => openEdit(current)}>
                        编辑
                      </button>
                    </div>
                  </div>
                ) : (
                  <p className="model-empty">
                    还没有选择{KIND_LABEL[kind]}。从下面挑一个推荐服务商，或者添加自定义模型。
                  </p>
                )}
                {test.kind !== 'idle' && <TestBox test={test} />}
              </div>

              <section className="model-section">
                <div className="section-head">
                  <h3>推荐服务商</h3>
                  <span>先选服务商和模型，再点「使用」粘贴 API Key</span>
                </div>
                <div className="preset-grid">
                  {presets.map((preset) => {
                    const saved = savedForProvider(preset)
                    const selected = selectedForProvider(preset)
                    const savedCount = list.filter(
                      (m) =>
                        m.baseUrl === normalizeBaseUrl(preset.baseUrl) &&
                        preset.models.some((pm) => pm.id === m.model),
                    ).length
                    return (
                      <article
                        className="preset-card"
                        key={preset.id}
                        data-recommended={preset.recommended ? 'true' : undefined}
                      >
                        <div className="preset-top">
                          <div>
                            <b>{preset.label}</b>
                            <span className="preset-provider">{preset.provider}</span>
                          </div>
                          <div className="preset-tags">
                            {preset.recommended && <span className="preset-tag">推荐</span>}
                            {savedCount > 0 && (
                              <span className="preset-tag is-ready">
                                已配置{savedCount > 1 ? ` ${savedCount}` : ''}
                              </span>
                            )}
                          </div>
                        </div>

                        <div className="preset-select-row">
                          <select
                            className="preset-select"
                            value={selected.id}
                            aria-label={`选择${preset.label}模型`}
                            onChange={(e) =>
                              setSelections((prev) => ({
                                ...prev,
                                [preset.id]: e.target.value,
                              }))
                            }
                          >
                            {preset.models.map((m) => (
                              <option key={m.id} value={m.id}>
                                {m.label}
                              </option>
                            ))}
                          </select>
                          <code className="preset-model">{selected.id}</code>
                        </div>

                        <p>{selected.note || preset.note}</p>
                        <div className="preset-actions">
                          <button
                            className="btn small"
                            onClick={() => openPreset(preset, selected)}
                          >
                            {saved?.model === selected.id ? '编辑 Key' : '使用'}
                          </button>
                          <a
                            className="btn small ghost key-link"
                            href={preset.keyUrl}
                            target="_blank"
                            rel="noreferrer"
                            title={`打开${preset.provider} API Key 页面`}
                          >
                            获取 API Key ↗
                          </a>
                        </div>
                      </article>
                    )
                  })}
                </div>
              </section>

              <section className="model-section">
                <div className="section-head">
                  <h3>我的模型</h3>
                  <button className="btn small" onClick={openCustom}>
                    <IconPlus /> 自定义模型
                  </button>
                </div>
                {list.length === 0 ? (
                  <p className="model-empty">
                    还没有添加{KIND_LABEL[kind]}。添加后可以在对话框里随时切换。
                  </p>
                ) : (
                  <div className="model-list">
                    {list.map((model) => {
                      const active = settings.active[kind]
                        ? settings.active[kind] === model.id
                        : current?.id === model.id
                      return (
                        <div
                          className="model-row"
                          key={model.id}
                          data-active={active}
                        >
                          <button
                            className="model-pick"
                            onClick={() => selectModel(model)}
                            title={isModelReady(model) ? '设为当前模型' : '补全 API Key'}
                          >
                            <span className="model-dot" />
                            <span className="model-info">
                              <b>
                                {model.label}
                                {!isModelReady(model) && (
                                  <em className="model-bad">缺 Key</em>
                                )}
                              </b>
                              <span>
                                {providerName(model)} · <code>{model.model}</code>
                                {model.apiKey ? ` · Key ${maskKey(model.apiKey)}` : ' · 未填 Key'}
                              </span>
                            </span>
                          </button>
                          <div className="model-row-actions">
                            {model.keyUrl && (
                              <a
                                className="icon-link"
                                href={model.keyUrl}
                                target="_blank"
                                rel="noreferrer"
                                title="获取 API Key"
                              >
                                ↗
                              </a>
                            )}
                            <button className="link-btn" onClick={() => openEdit(model)}>
                              编辑
                            </button>
                            <button
                              className="link-btn danger"
                              onClick={() => removeModel(model)}
                            >
                              删除
                            </button>
                          </div>
                        </div>
                      )
                    })}
                  </div>
                )}
              </section>
            </>
          )}
        </div>
        {editor && (
          <div className="settings-editor-actions">
            <button className="btn" onClick={closeEditor}>
              取消
            </button>
            <button
              className="btn"
              onClick={() => void testModel(draftProfile(editor))}
              disabled={!draftReady || test.kind === 'testing'}
            >
              测试连接
            </button>
            <button className="btn primary" onClick={saveEditor} disabled={!draftReady}>
              保存并使用
            </button>
          </div>
        )}
      </>
    ) : tab === 'translation' ? (
      <TranslationPanel />
    ) : tab === 'reading' ? (
      <ReadingPanel />
    ) : tab === 'scholar' ? (
      <ScholarConfigPanel />
    ) : tab === 'tools' ? (
      <ToolchainPanel />
    ) : tab === 'review' ? (
      <ReviewTemplatePanel />
    ) : tab === 'tour' && tourVisible ? (
      <TourPanel onClose={requestClose} onDismissed={hideTour} />
    ) : tab === 'server' ? (
      <MobileServerPanel />
    ) : (
              <div className="modal-body update-panel" ref={bodyRef}>
                <section className="update-current">
                  <div className="update-current-info">
                    <span className="update-label">当前版本</span>
                    <div className="update-current-line">
                      <b className="update-version">v{updateInfo?.current || '—'}</b>
                      {updateChecking ? (
                        <span className="update-status is-muted">检查中…</span>
                      ) : updateError ? (
                        <span className="update-status is-warn">检查失败</span>
                      ) : updateInfo?.has_update ? (
                        <span className="update-status is-new">
                          可更新至 v{updateInfo.latest}
                        </span>
                      ) : updateInfo ? (
                        <span className="update-status is-latest">
                          <IconCheck /> 已是最新版本
                        </span>
                      ) : (
                        <span className="update-status is-muted">尚未检查</span>
                      )}
                    </div>
                    <span className="update-time">
                      上次检查：{formatCheckTime(updateCheckedAt)}
                      {updateInfo?.cached ? ' · 当天缓存' : ''}
                    </span>
                  </div>
                  <button
                    className="btn update-check-btn"
                    onClick={() => void runUpdateCheck(true)}
                    disabled={updateChecking}
                  >
                    {updateChecking ? '检查中…' : '检查更新'}
                  </button>
                </section>

                {updateError && (
                  <div className="update-error">
                    检查失败：{updateError}
                    <br />
                    可以稍后重试，或直接打开发布页查看。
                  </div>
                )}

                {updateInfo?.has_update && !updateError && (
                  <section className="update-release">
                    <div className="update-release-head">
                      <b>v{updateInfo.latest}</b>
                      {updateInfo.pub_date && (
                        <span>{updateInfo.pub_date.slice(0, 10)}</span>
                      )}
                    </div>
                    {updateInfo.notes && (
                      <div className="update-notes-card">
                        <div className="update-notes-head">更新内容</div>
                        <ul className="update-notes-list">
                          {parseUpdateNotes(updateInfo.notes).map((line, i) => (
                            <li key={i}>{line}</li>
                          ))}
                        </ul>
                      </div>
                    )}
                    <div className="update-release-actions">
                      {canInstallInApp() ? (
                        <button
                          className="btn primary"
                          onClick={() => void installUpdate()}
                          disabled={installing}
                        >
                          {installing
                            ? '正在下载并安装…'
                            : `立即更新到 v${updateInfo.latest}`}
                        </button>
                      ) : (
                        <a
                          className="btn primary"
                          href={updateInfo.url || updateInfo.release_url}
                          target="_blank"
                          rel="noreferrer"
                        >
                          下载安装包 ↗
                        </a>
                      )}
                      {canInstallInApp() && !installing && (
                        <a
                          className="update-alt-link"
                          href={updateInfo.url || updateInfo.release_url}
                          target="_blank"
                          rel="noreferrer"
                        >
                          手动下载
                        </a>
                      )}
                    </div>
                    {installing && (
                      <p className="update-install-hint">
                        下载完成后应用会自动关闭并启动安装程序，请勿关闭窗口。
                      </p>
                    )}
                    {installError && (
                      <div className="update-error">自动更新失败：{installError}</div>
                    )}
                  </section>
                )}

                <p className="update-meta">
                  <span>应用每天自动检查一次</span>
                  <span aria-hidden="true">·</span>
                  <span>
                    {canInstallInApp() ? '桌面版支持应用内更新' : '浏览器版需手动下载安装包'}
                  </span>
                  <a
                    href={
                      updateInfo?.release_url ||
                      'https://github.com/xiaohui-plus/Paperead/releases/latest'
                    }
                    target="_blank"
                    rel="noreferrer"
                  >
                    打开发布页 ↗
                  </a>
                </p>
              </div>
            )}
        </main>
      </div>
    </div>
  )
}

/** 设置 → 新手教程：桌面端手动重看或继续上次进度。 */
function TourPanel({
  onClose,
  onDismissed,
}: {
  onClose: () => void
  onDismissed: () => void
}) {
  const [state, setState] = useState<OnboardingState | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    void getOnboarding()
      .then(setState)
      .catch(() => setState(null))
  }, [])

  const inProgress = state?.status === 'in_progress' && Boolean(state.stepId)
  const label = inProgress
    ? '继续上次的教程'
    : state?.status === 'completed' || state?.status === 'skipped'
      ? '重新观看教程'
      : '开始新手教程'

  const start = () => {
    window.dispatchEvent(
      new CustomEvent('paperead:tour-start', {
        detail: { resume: inProgress },
      }),
    )
    onClose()
  }

  const dismiss = async () => {
    if (busy) return
    setBusy(true)
    setError('')
    try {
      await postOnboarding('dismiss')
      // 如果教程正开着，一起收起，避免关掉设置后又弹出
      window.dispatchEvent(new Event('paperead:tour-stop'))
      // 设置里也不再保留新手教程入口
      onDismissed()
    } catch {
      setError('操作失败，请重试')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal-body tour-settings">
      <div className="tour-settings-card">
        <span className="tour-settings-icon">
          <IconSpark />
        </span>
        <div>
          <h3>{inProgress ? '继续上次的新手教程' : '第一次使用 Paperead？'}</h3>
          <p>
            教程会贴着真实界面走一遍：上传论文、生成导读、找文献、建研究项目、写一篇综述。
            每一步完成后再进入下一步，不会一次塞给你一堆说明。
          </p>
        </div>
      </div>
      <div className="tour-settings-actions">
        <button className="btn primary tour-settings-start" onClick={start}>
          {label}
        </button>
        {inProgress && (
          <button
            className="tour-settings-dismiss"
            onClick={() => void dismiss()}
            disabled={busy}
          >
            {busy ? '处理中…' : '不再提醒'}
          </button>
        )}
      </div>
      {error && <p className="tour-settings-error">{error}</p>}
      <p className="tour-settings-note">
        只会在桌面客户端首次启动时自动出现一次；网页端不显示这个入口。
      </p>
    </div>
  )
}

/** 设置 → 文献检索：Semantic Scholar / CORE 检索需要的 API Key。 */
function ScholarKeyCard({
  title,
  description,
  inputId,
  value,
  placeholder,
  show,
  onChange,
  onToggle,
  link,
  linkLabel,
  envVar,
}: {
  title: string
  description: string
  inputId: string
  value: string
  placeholder: string
  show: boolean
  onChange: (value: string) => void
  onToggle: () => void
  link: string
  linkLabel: string
  envVar: string
}) {
  const configured = value.trim().length > 0
  return (
    <section className="scholar-card">
      <header className="scholar-card-head">
        <div className="scholar-card-title">
          <h3>{title}</h3>
          <span className="scholar-badge" data-ready={configured}>
            {configured ? '已配置' : '未配置'}
          </span>
        </div>
        <a
          className="btn small key-link"
          href={link}
          target="_blank"
          rel="noreferrer"
        >
          {linkLabel} ↗
        </a>
      </header>
      <p className="scholar-card-desc">{description}</p>
      <label className="field-label" htmlFor={inputId}>
        API Key
      </label>
      <div className="input-row">
        <input
          id={inputId}
          className="input"
          type={show ? 'text' : 'password'}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          placeholder={placeholder}
          spellCheck={false}
          autoComplete="off"
        />
        <button className="ghost-btn" onClick={onToggle}>
          {show ? '隐藏' : '显示'}
        </button>
      </div>
      <p className="hint">
        密钥只保存在这台机器的 localStorage，服务端不落盘；也可以用环境变量{' '}
        <code>{envVar}</code> 预先配置，应用内的设置优先。
      </p>
    </section>
  )
}

function ScholarConfigPanel() {
  const initial = loadScholarSettings()
  const [key, setKey] = useState(initial.coreApiKey)
  const [s2Key, setS2Key] = useState(initial.s2ApiKey)
  const [show, setShow] = useState(false)
  const [showS2, setShowS2] = useState(false)
  const [saved, setSaved] = useState(false)

  const save = () => {
    saveScholarSettings({ coreApiKey: key.trim(), s2ApiKey: s2Key.trim() })
    setSaved(true)
    window.setTimeout(() => setSaved(false), 2400)
  }

  return (
    <div className="modal-body scholar-config">
      <p className="scholar-intro">
        这里配置的是文献检索的数据源密钥；不填也能使用免 Key 的来源，填写后额度更高、结果更稳定。
      </p>
      <ScholarKeyCard
        title="Semantic Scholar"
        description="提供全学科检索与开放获取 PDF，不填也能用；配置 Key 后额度更高、检索更稳定。"
        inputId="s2-api-key"
        value={s2Key}
        placeholder="粘贴 Semantic Scholar API Key（可选）"
        show={showS2}
        onChange={setS2Key}
        onToggle={() => setShowS2((value) => !value)}
        link="https://www.semanticscholar.org/product/api"
        linkLabel="去申请 API Key"
        envVar="PAPEREAD_S2_API_KEY"
      />
      <ScholarKeyCard
        title="CORE"
        description="聚合开放获取论文的全文与元数据。填写 Key 后文献检索会启用 CORE 数据源，留空则隐藏该来源。"
        inputId="core-api-key"
        value={key}
        placeholder="粘贴 CORE API Key"
        show={show}
        onChange={setKey}
        onToggle={() => setShow((value) => !value)}
        link="https://core.ac.uk/services/api"
        linkLabel="去 CORE 申请 API Key"
        envVar="PAPEREAD_CORE_API_KEY"
      />
      <div className="scholar-config-actions">
        <button className="btn primary" onClick={save}>
          保存设置
        </button>
        {saved && <span className="scholar-saved">已保存，回到检索页即可生效</span>}
      </div>
    </div>
  )
}

/** 设置 → 译文语言：只影响阅读页翻译，问答与导读仍用中文。 */
function TranslationPanel() {
  const [lang, setLang] = useState(loadTargetLang)

  return (
    <div className="modal-body">
      <section className="model-section">
        <div className="section-head">
          <h3>阅读页译文语言</h3>
        </div>
        <p className="model-empty">
          英文论文会翻译成这里选择的语言。术语表、引用编号、公式和数字都会原样保留；
          问答、导读和脉络不受影响，仍按中文回答。
        </p>
        <label className="field-label" htmlFor="target-lang">
          翻译成
        </label>
        <select
          id="target-lang"
          className="input"
          value={lang}
          onChange={(event) => {
            setLang(event.target.value)
            saveTargetLang(event.target.value)
          }}
        >
          {TRANSLATION_LANGS.map((item) => (
            <option key={item.key} value={item.key}>
              {item.label}
            </option>
          ))}
        </select>
        <p className="hint">
          切换后，已经翻译过的论文会在下次打开时按新语言重新翻译；原文、高亮和笔记不受影响。
          只对当前这台机器生效。
        </p>
      </section>
    </div>
  )
}

/** 设置 → 自动阅读：控制是否在翻译后自动提取研究目标 / 方法 / 结论。 */
function ReadingPanel() {
  const [enabled, setEnabled] = useState(loadAutoHighlights)

  return (
    <div className="modal-body">
      <section className="model-section">
        <div className="section-head">
          <h3>阅读时自动提取重点</h3>
        </div>
        <p className="model-empty">
          每翻完一页，自动识别这一页里的研究目标、研究方法和主要结论，用蓝、绿、橙三种颜色标在原文和译文上，
          同时摘录到左侧「阅读重点」。论文里没写的内容不会硬标。
        </p>

        <div className="setting-row">
          <div className="setting-row-text">
            <b>自动标记阅读重点</b>
            <span>
              关闭后不再自动生成新的标记，也不会发起额外的模型调用；已经生成的重点仍会保留。
              每个看过的页会多 1 次模型调用。
            </span>
          </div>
          <button
            type="button"
            role="switch"
            aria-checked={enabled}
            className="switch"
            data-on={enabled}
            onClick={() => {
              const next = !enabled
              setEnabled(next)
              saveAutoHighlights(next)
            }}
            title={enabled ? '关闭自动标记' : '开启自动标记'}
          >
            <i />
          </button>
        </div>

        <p className="hint">
          这个开关只影响自动标记；你自己划词的高亮和笔记不受影响。
        </p>
      </section>
    </div>
  )
}
