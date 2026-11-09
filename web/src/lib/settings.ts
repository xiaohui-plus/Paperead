/**
 * 模型配置：回答模型与识图模型分开保存。
 *
 * - 回答模型（answer）：论文问答、翻译、导读、脉络等所有文字任务。
 * - 识图模型（vision）：论文阅读里的图片理解（公式裁图 → LaTeX）。
 *
 * 两套配置可以指向不同服务商；密钥只存在本机 localStorage，随请求以 body
 * 发给本地服务，服务端不落盘。
 */

export type ModelKind = 'answer' | 'vision'

export interface ModelProfile {
  id: string
  kind: ModelKind
  /** 展示用名称，预设模型用服务商给出的名字，自定义模型默认用模型名 */
  label: string
  baseUrl: string
  model: string
  apiKey: string
  /** 预设 id，或 'custom' */
  provider: string
  /** 该服务商获取 API Key 的官方页面 */
  keyUrl?: string
  createdAt: number
  updatedAt: number
}

export interface ModelSettings {
  version: 2
  models: ModelProfile[]
  /** 每种角色当前选中的模型 id；空串表示未选择 */
  active: Record<ModelKind, string>
}

export interface ProviderModel {
  /** 实际发给接口的模型 ID */
  id: string
  label: string
  note: string
  recommended?: boolean
}

export interface ProviderPreset {
  id: string
  kind: ModelKind
  /** 服务商简称，如 DeepSeek */
  label: string
  /** 服务商全称，如 DeepSeek 开放平台 */
  provider: string
  baseUrl: string
  note: string
  /** 官方「创建 / 查看 API Key」页面 */
  keyUrl: string
  recommended?: boolean
  /** 下拉框默认选中的模型 */
  defaultModel: string
  /** 该服务商当前提供的模型 */
  models: ProviderModel[]
}

/** 从接口配置发给本地服务的形状（snake_case，与后端 LLMConfigIn 对齐）。 */
export interface LLMPayload {
  base_url: string
  api_key: string
  model: string
}

export const KIND_LABEL: Record<ModelKind, string> = {
  answer: '回答模型',
  vision: '识图模型',
}

export const KIND_HINT: Record<ModelKind, string> = {
  answer:
    '负责论文问答、翻译、导读和脉络；推荐已换到 2026 新模型。要多模态选 DeepSeek / Kimi / 豆包，偏纯文本推理选 GLM-5.3 / Qwen。',
  vision:
    '负责论文里的图片理解（公式裁图 → LaTeX）；推荐用原生多模态新模型，也可以选专用视觉模型。',
}

/**
 * 推荐模型（2026-11 核对）。
 * 价格和模型名会随服务商调整，接入前以官方控制台为准；界面上的
 * 「获取 API Key」会直接跳到官方创建密钥页面。
 */
export const PRESETS: ProviderPreset[] = [
  // ── 回答模型：每个服务商一个下拉框 ─────────────────────
  {
    id: 'deepseek',
    kind: 'answer',
    label: 'DeepSeek',
    provider: 'DeepSeek 开放平台',
    baseUrl: 'https://api.deepseek.com/v1',
    note: 'V4.1 系列，原生多模态，价格低',
    keyUrl: 'https://platform.deepseek.com/api_keys',
    recommended: true,
    defaultModel: 'deepseek-flash',
    models: [
      {
        id: 'deepseek-flash',
        label: 'DeepSeek V4.1 Flash',
        note: '2026-09 最新，原生多模态；速度快、价格低',
        recommended: true,
      },
      {
        id: 'deepseek-v4-pro',
        label: 'DeepSeek V4 Pro',
        note: '上一代更强推理；官方正在逐步并入 V4.1 Flash',
      },
    ],
  },
  {
    id: 'zhipu',
    kind: 'answer',
    label: '智谱',
    provider: '智谱开放平台',
    baseUrl: 'https://open.bigmodel.cn/api/paas/v4',
    note: 'GLM-5 系列，中文长文强',
    keyUrl: 'https://open.bigmodel.cn/usercenter/apikeys',
    recommended: true,
    defaultModel: 'glm-5.3',
    models: [
      {
        id: 'glm-5.3',
        label: 'GLM-5.3',
        note: '2026 最新旗舰；1M 上下文、深度思考（纯文本）',
        recommended: true,
      },
      { id: 'glm-5.2', label: 'GLM-5.2', note: '上一代旗舰，稳定' },
      { id: 'glm-5', label: 'GLM-5', note: '更低成本的通用版' },
      { id: 'glm-4.6', label: 'GLM-4.6', note: '旧版，兼容老接入' },
    ],
  },
  {
    id: 'kimi',
    kind: 'answer',
    label: 'Kimi',
    provider: 'Kimi 开放平台',
    baseUrl: 'https://api.moonshot.cn/v1',
    note: 'K3.1 系列，多模态、1M 上下文',
    keyUrl: 'https://platform.kimi.com/console/api-keys',
    recommended: true,
    defaultModel: 'kimi-k3.1-pro',
    models: [
      {
        id: 'kimi-k3.1-pro',
        label: 'Kimi K3.1 Pro',
        note: '2026-11 最新旗舰；原生多模态，1M 上下文',
        recommended: true,
      },
      {
        id: 'kimi-k3.1-pro-highspeed',
        label: 'Kimi K3.1 Pro 高速版',
        note: '同等能力，约 2–3 倍速度',
      },
      { id: 'kimi-k3', label: 'Kimi K3', note: '上一代旗舰，多模态' },
    ],
  },
  {
    id: 'doubao',
    kind: 'answer',
    label: '豆包',
    provider: '火山方舟',
    baseUrl: 'https://ark.cn-beijing.volces.com/api/v3',
    note: 'Seed 2.0 系列，原生多模态',
    keyUrl: 'https://console.volcengine.com/ark/region:ark+cn-beijing/apiKey',
    recommended: true,
    defaultModel: 'doubao-seed-2-0-pro',
    models: [
      {
        id: 'doubao-seed-2-0-pro',
        label: '豆包 Seed 2.0 Pro',
        note: '2026 最新；原生多模态，图表/长文理解强',
        recommended: true,
      },
      {
        id: 'doubao-seed-2-0-lite',
        label: '豆包 Seed 2.0 Lite',
        note: '更便宜、更快，适合日常问答',
      },
    ],
  },
  {
    id: 'qwen',
    kind: 'answer',
    label: '通义千问',
    provider: '阿里云百炼',
    baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    note: 'Qwen3-Max 推理强（纯文本）',
    keyUrl: 'https://bailian.console.aliyun.com/?apiKey=1#/api-key',
    defaultModel: 'qwen3-max',
    models: [
      {
        id: 'qwen3-max',
        label: 'Qwen3-Max-Thinking',
        note: '官方当前可用的最新 Max；纯文本推理强',
        recommended: true,
      },
      { id: 'qwen-plus', label: 'Qwen-Plus', note: '便宜通用，适合大量翻译' },
    ],
  },
  {
    id: 'siliconflow',
    kind: 'answer',
    label: '硅基流动',
    provider: 'SiliconFlow 平台',
    baseUrl: 'https://api.siliconflow.cn/v1',
    note: '一个 Key 切换 2026 新模型：DeepSeek V4.1 / GLM-5.3 / Qwen3.8 / Kimi K2.7',
    keyUrl: 'https://cloud.siliconflow.cn/account/ak',
    recommended: true,
    defaultModel: 'deepseek-ai/DeepSeek-V4.1-Flash',
    models: [
      {
        id: 'deepseek-ai/DeepSeek-V4.1-Flash',
        label: 'DeepSeek V4.1 Flash',
        note: '2026-09 最新；原生多模态，性价比最高',
        recommended: true,
      },
      {
        id: 'zai-org/GLM-5.3-Flash',
        label: 'GLM-5.3-Flash',
        note: '2026-08 最新 GLM；原生多模态，1M 上下文',
        recommended: true,
      },
      {
        id: 'Qwen/Qwen3.8-27B',
        label: 'Qwen3.8-27B',
        note: '2026-08 最新 Qwen；原生视觉，256K 上下文',
        recommended: true,
      },
      {
        id: 'moonshotai/Kimi-K2.7-Code',
        label: 'Kimi K2.7 Code',
        note: '2026-06；多模态，代码/Agent 强',
        recommended: true,
      },
      {
        id: 'zai-org/GLM-5.3',
        label: 'GLM-5.3',
        note: '2026-08 旗舰；纯文本，复杂推理强',
      },
      {
        id: 'tencent/Hy4-preview',
        label: '腾讯 Hy4 Preview',
        note: '2026-08 混元新一代；1M 上下文（Preview）',
      },
      {
        id: 'deepseek-ai/DeepSeek-V4-Pro',
        label: 'DeepSeek V4 Pro',
        note: '2026-04 旗舰；更强推理，价格更高',
      },
      {
        id: 'Qwen/Qwen3.6-27B',
        label: 'Qwen3.6-27B',
        note: '上一代稳定通用',
      },
      {
        id: 'Qwen/Qwen3.6-35B-A3B',
        label: 'Qwen3.6-35B-A3B',
        note: 'MoE 版本，便宜、吞吐高',
      },
      {
        id: 'XingChenAGI/Xing4.0-29B',
        label: '星尘 Xing4.0-29B',
        note: '2026-09 国产开源，便宜',
      },
    ],
  },

  // ── 识图模型：同样按服务商分组 ─────────────────────────
  {
    id: 'deepseek-vision',
    kind: 'vision',
    label: 'DeepSeek',
    provider: 'DeepSeek 开放平台',
    baseUrl: 'https://api.deepseek.com/v1',
    note: 'V4.1 原生视觉理解',
    keyUrl: 'https://platform.deepseek.com/api_keys',
    recommended: true,
    defaultModel: 'deepseek-flash',
    models: [
      {
        id: 'deepseek-flash',
        label: 'DeepSeek V4.1 Flash',
        note: '原生视觉；公式、图表、表格都能识',
        recommended: true,
      },
    ],
  },
  {
    id: 'zhipu-vision',
    kind: 'vision',
    label: '智谱',
    provider: '智谱开放平台',
    baseUrl: 'https://open.bigmodel.cn/api/paas/v4',
    note: 'GLM 视觉模型',
    keyUrl: 'https://open.bigmodel.cn/usercenter/apikeys',
    recommended: true,
    defaultModel: 'glm-4.6v',
    models: [
      {
        id: 'glm-4.6v',
        label: 'GLM-4.6V',
        note: '智谱当前视觉模型，公式识别性价比高',
        recommended: true,
      },
    ],
  },
  {
    id: 'kimi-vision',
    kind: 'vision',
    label: 'Kimi',
    provider: 'Kimi 开放平台',
    baseUrl: 'https://api.moonshot.cn/v1',
    note: 'K3.1 原生多模态',
    keyUrl: 'https://platform.kimi.com/console/api-keys',
    recommended: true,
    defaultModel: 'kimi-k3.1-pro',
    models: [
      {
        id: 'kimi-k3.1-pro',
        label: 'Kimi K3.1 Pro',
        note: '原生视觉，1M 上下文，适合读复杂图表',
        recommended: true,
      },
      {
        id: 'kimi-k3.1-pro-highspeed',
        label: 'Kimi K3.1 Pro 高速版',
        note: '同等视觉能力，约 2–3 倍速度',
      },
      { id: 'kimi-k3', label: 'Kimi K3', note: '上一代多模态旗舰' },
    ],
  },
  {
    id: 'doubao-vision',
    kind: 'vision',
    label: '豆包',
    provider: '火山方舟',
    baseUrl: 'https://ark.cn-beijing.volces.com/api/v3',
    note: 'Seed 2.0 原生多模态',
    keyUrl: 'https://console.volcengine.com/ark/region:ark+cn-beijing/apiKey',
    recommended: true,
    defaultModel: 'doubao-seed-2-0-pro',
    models: [
      {
        id: 'doubao-seed-2-0-pro',
        label: '豆包 Seed 2.0 Pro',
        note: '图表理解强，适合复杂插图/表格',
        recommended: true,
      },
      {
        id: 'doubao-seed-2-0-lite',
        label: '豆包 Seed 2.0 Lite',
        note: '更便宜、更快',
      },
    ],
  },
  {
    id: 'qwen-vision',
    kind: 'vision',
    label: '通义千问',
    provider: '阿里云百炼',
    baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    note: 'Qwen3-VL 专用视觉',
    keyUrl: 'https://bailian.console.aliyun.com/?apiKey=1#/api-key',
    recommended: true,
    defaultModel: 'qwen3-vl-plus',
    models: [
      {
        id: 'qwen3-vl-plus',
        label: 'Qwen3-VL-Plus',
        note: '专用视觉模型，复杂公式、表格更稳',
        recommended: true,
      },
      {
        id: 'qwen3-vl-flash',
        label: 'Qwen3-VL-Flash',
        note: '更便宜、更快，适合批量公式',
      },
    ],
  },
  {
    id: 'siliconflow-vision',
    kind: 'vision',
    label: '硅基流动',
    provider: 'SiliconFlow 平台',
    baseUrl: 'https://api.siliconflow.cn/v1',
    note: '一个 Key 切换 2026 多模态新模型；列表外模型用自定义',
    keyUrl: 'https://cloud.siliconflow.cn/account/ak',
    recommended: true,
    defaultModel: 'deepseek-ai/DeepSeek-V4.1-Flash',
    models: [
      {
        id: 'deepseek-ai/DeepSeek-V4.1-Flash',
        label: 'DeepSeek V4.1 Flash',
        note: '2026-09 最新；原生视觉，通用识图/公式',
        recommended: true,
      },
      {
        id: 'zai-org/GLM-5.3-Flash',
        label: 'GLM-5.3-Flash',
        note: '2026-08 最新 GLM；原生多模态',
        recommended: true,
      },
      {
        id: 'Qwen/Qwen3.8-27B',
        label: 'Qwen3.8-27B',
        note: '2026-08 最新 Qwen；原生视觉，256K',
        recommended: true,
      },
      {
        id: 'moonshotai/Kimi-K2.7-Code',
        label: 'Kimi K2.7 Code',
        note: '2026-06；支持图片/视频输入',
        recommended: true,
      },
      {
        id: 'Qwen/Qwen3-VL-32B-Instruct',
        label: 'Qwen3-VL-32B-Instruct',
        note: '32B 专用视觉，复杂公式、表格更稳',
      },
      {
        id: 'Qwen/Qwen3-VL-8B-Instruct',
        label: 'Qwen3-VL-8B-Instruct',
        note: '更便宜、更快，适合批量公式',
      },
    ],
  },
]

/** 自定义模型时可一键填入的常见 OpenAI 兼容地址。 */
export const BASE_URL_OPTIONS: { label: string; url: string }[] = [
  { label: 'DeepSeek', url: 'https://api.deepseek.com/v1' },
  { label: '智谱', url: 'https://open.bigmodel.cn/api/paas/v4' },
  { label: 'Kimi', url: 'https://api.moonshot.cn/v1' },
  { label: '豆包', url: 'https://ark.cn-beijing.volces.com/api/v3' },
  { label: '通义', url: 'https://dashscope.aliyuncs.com/compatible-mode/v1' },
  { label: '硅基流动', url: 'https://api.siliconflow.cn/v1' },
  { label: 'OpenAI', url: 'https://api.openai.com/v1' },
]

const STORAGE_KEY = 'paperead.models'
const LEGACY_KEYS = ['paperead.llm', 'paper-reflow.llm']

export function presetsFor(kind: ModelKind): ProviderPreset[] {
  return PRESETS.filter((p) => p.kind === kind)
}

export function presetById(id: string): ProviderPreset | undefined {
  return PRESETS.find((p) => p.id === id)
}

/** 在某个服务商的模型列表里找模型；找不到返回第一个。 */
export function presetModel(
  preset: ProviderPreset,
  modelId?: string,
): ProviderModel {
  return (
    preset.models.find((m) => m.id === modelId) ??
    preset.models.find((m) => m.id === preset.defaultModel) ??
    preset.models[0]
  )
}

export function normalizeBaseUrl(url: string): string {
  return url.trim().replace(/\/+$/, '')
}

export function newModelId(): string {
  const c = globalThis.crypto as Crypto | undefined
  if (c?.randomUUID) return c.randomUUID()
  return `m_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`
}

function emptySettings(): ModelSettings {
  return { version: 2, models: [], active: { answer: '', vision: '' } }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function normalizeSettings(raw: unknown): ModelSettings {
  if (!isRecord(raw)) return emptySettings()
  const models: ModelProfile[] = []
  for (const item of Array.isArray(raw.models) ? raw.models : []) {
    if (!isRecord(item)) continue
    if (item.kind !== 'answer' && item.kind !== 'vision') continue
    const baseUrl = normalizeBaseUrl(String(item.baseUrl ?? ''))
    const model = String(item.model ?? '').trim()
    if (!baseUrl || !model) continue
    const id = String(item.id ?? '') || newModelId()
    models.push({
      id,
      kind: item.kind,
      label: String(item.label ?? '').trim() || model,
      baseUrl,
      model,
      apiKey: String(item.apiKey ?? ''),
      provider: String(item.provider ?? 'custom'),
      keyUrl: item.keyUrl ? String(item.keyUrl) : undefined,
      createdAt: Number(item.createdAt) || Date.now(),
      updatedAt: Number(item.updatedAt) || Date.now(),
    })
  }

  const activeRaw = isRecord(raw.active) ? raw.active : {}
  const active: Record<ModelKind, string> = { answer: '', vision: '' }
  for (const kind of ['answer', 'vision'] as const) {
    const id = String(activeRaw[kind] ?? '')
    active[kind] = models.some((m) => m.id === id && m.kind === kind) ? id : ''
  }

  return { version: 2, models, active }
}

/**
 * 旧版本只有一套 `{ baseUrl, apiKey, model }`，问答、翻译、公式共用。
 * 迁移时把它同时放进两个角色，避免升级后翻译或公式识别突然失灵；
 * 用户之后可以分别在两个 Tab 里换成更合适的模型。
 */
function migrateLegacy(): ModelSettings | null {
  let raw: unknown
  try {
    const text = LEGACY_KEYS.map((key) => localStorage.getItem(key)).find(
      Boolean,
    )
    if (!text) return null
    raw = JSON.parse(text)
  } catch {
    return null
  }
  if (!isRecord(raw)) return null

  const baseUrl = normalizeBaseUrl(String(raw.baseUrl ?? ''))
  const model = String(raw.model ?? '').trim()
  const apiKey = String(raw.apiKey ?? '').trim()
  if (!baseUrl || !model) return null
  if (!apiKey) return emptySettings()

  const matchedProvider = PRESETS.find(
    (p) => p.baseUrl === baseUrl && p.models.some((m) => m.id === model),
  )
  const matchedModel = matchedProvider?.models.find((m) => m.id === model)
  const now = Date.now()
  const make = (kind: ModelKind): ModelProfile => ({
    id: newModelId(),
    kind,
    label: matchedModel?.label ?? model,
    baseUrl,
    model,
    apiKey,
    provider: matchedProvider?.id ?? 'custom',
    keyUrl: matchedProvider?.keyUrl,
    createdAt: now,
    updatedAt: now,
  })

  const answer = make('answer')
  // 只有看起来像视觉模型时才复制一份到识图角色，避免把纯文本模型
  // 显示成「已配置的识图模型」；旧版预设多数是视觉模型，会命中。
  const visionProvider = PRESETS.find(
    (p) =>
      p.kind === 'vision' &&
      p.baseUrl === baseUrl &&
      p.models.some((m) => m.id === model),
  )
  const looksVision =
    Boolean(visionProvider) ||
    /(?:vl|vision|4o|omni|seed|flash|kimi)/i.test(model)
  const vision = looksVision ? make('vision') : null

  return {
    version: 2,
    models: vision ? [answer, vision] : [answer],
    active: { answer: answer.id, vision: vision?.id ?? '' },
  }
}

export function loadModelSettings(): ModelSettings {
  try {
    const text = localStorage.getItem(STORAGE_KEY)
    if (text) return normalizeSettings(JSON.parse(text))
  } catch {
    // 坏数据当作没配置，下面再尝试旧版迁移
  }

  const migrated = migrateLegacy()
  if (migrated) {
    saveModelSettings(migrated)
    return migrated
  }
  return emptySettings()
}

export function saveModelSettings(settings: ModelSettings): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(settings))
  } catch {
    // 配额满或隐私模式：保持内存状态可用，但下次打开会丢配置
  }
}

/** 某个角色当前选中的模型（可能还没填 Key）。 */
export function activeModel(settings: ModelSettings, kind: ModelKind): ModelProfile | null {
  const exact = settings.models.find(
    (m) => m.id === settings.active[kind] && m.kind === kind,
  )
  if (exact) return exact
  return settings.models.find((m) => m.kind === kind) ?? null
}

/** 某个角色拿来真正发请求的模型：优先当前选中，坏了就退到第一个可用的。 */
export function readyModel(settings: ModelSettings, kind: ModelKind): ModelProfile | null {
  const active = activeModel(settings, kind)
  if (isModelReady(active)) return active
  return settings.models.find((m) => m.kind === kind && isModelReady(m)) ?? null
}

export function isModelReady(model: ModelProfile | null | undefined): boolean {
  // 后端 LLMConfigIn.api_key 要求至少 8 位，这里提前拦住，避免保存后才报错
  return Boolean(
    model &&
      model.baseUrl.trim() &&
      model.model.trim() &&
      model.apiKey.trim().length >= 8,
  )
}

/** 读取当前可用的回答模型。所有文字任务都应该走这里。 */
export function answerModel(): ModelProfile | null {
  return readyModel(loadModelSettings(), 'answer')
}

/** 读取当前可用的识图模型。所有图片识别任务都应该走这里。 */
export function visionModel(): ModelProfile | null {
  return readyModel(loadModelSettings(), 'vision')
}

/**
 * 把识图模型配置推给本地服务，供解析阶段把公式裁图转成 LaTeX。
 * 服务端只放在内存里，不落盘；本地服务没起时静默忽略。
 */
export async function syncVisionConfig(): Promise<void> {
  try {
    const model = visionModel()
    await fetch('/api/config/vision', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ config: model ? toPayload(model) : null }),
    })
  } catch {
    // 本地服务未启动：下次启动或保存设置时会再同步
  }
}

export function isAnswerConfigured(): boolean {
  return Boolean(answerModel())
}

export function toPayload(model: ModelProfile): LLMPayload {
  return {
    base_url: normalizeBaseUrl(model.baseUrl),
    api_key: model.apiKey.trim(),
    model: model.model.trim(),
  }
}

export function maskKey(key: string): string {
  if (!key) return ''
  if (key.length <= 10) return '••••'
  return `${key.slice(0, 4)}••••${key.slice(-4)}`
}

export function providerName(model: ModelProfile): string {
  // 旧版本的 provider 存的是单个模型预设 id，换成按 baseUrl 回退，
  // 避免升级后「我的模型」里显示 deepseek-v4.1-flash 这种内部 id。
  const preset =
    PRESETS.find((p) => p.id === model.provider) ??
    PRESETS.find((p) => p.baseUrl === normalizeBaseUrl(model.baseUrl))
  if (preset) return preset.provider
  if (model.provider === 'custom') return '自定义'
  return model.provider
}

/** 后端 `/api/ping`、`/api/formula` 共用的配置体。 */
export function configBody(model: ModelProfile): {
  base_url: string
  api_key: string
  model: string
} {
  return toPayload(model)
}
