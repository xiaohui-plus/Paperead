/** LLM 配置：存浏览器 localStorage，随请求发给本地服务，服务端不落盘。 */

export interface LLMSettings {
  baseUrl: string
  apiKey: string
  model: string
}

export interface Preset {
  id: string
  label: string
  baseUrl: string
  model: string
  note: string
}

/**
 * 国内可用的视觉模型（2026-11 核对）。
 * 价格会变，接入前以官方控制台为准。
 */
export const PRESETS: Preset[] = [
  {
    id: 'glm',
    label: '智谱 GLM-4.6V',
    baseUrl: 'https://open.bigmodel.cn/api/paas/v4',
    model: 'glm-4.6v',
    note: '输出最便宜（$0.9/M），开源可自托管',
  },
  {
    id: 'qwen',
    label: '通义 Qwen3-VL-Plus',
    baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    model: 'qwen3-vl-plus',
    note: '视觉能力更强，适合复杂公式',
  },
  {
    id: 'doubao',
    label: '豆包 Seed 2.0 Lite',
    baseUrl: 'https://ark.cn-beijing.volces.com/api/v3',
    model: 'doubao-seed-2-0-lite',
    note: '全模态，输出价略高',
  },
]

const STORAGE_KEY = 'paper-reflow.llm'

export function loadSettings(): LLMSettings {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return { baseUrl: PRESETS[0].baseUrl, apiKey: '', model: PRESETS[0].model }
    const parsed = JSON.parse(raw) as Partial<LLMSettings>
    return {
      baseUrl: parsed.baseUrl || PRESETS[0].baseUrl,
      apiKey: parsed.apiKey || '',
      model: parsed.model || PRESETS[0].model,
    }
  } catch {
    return { baseUrl: PRESETS[0].baseUrl, apiKey: '', model: PRESETS[0].model }
  }
}

export function saveSettings(s: LLMSettings): void {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(s))
}

export function isConfigured(s: LLMSettings): boolean {
  return Boolean(s.baseUrl && s.apiKey && s.model)
}

export function maskKey(key: string): string {
  if (key.length <= 10) return key ? '••••' : ''
  return `${key.slice(0, 4)}••••${key.slice(-4)}`
}
