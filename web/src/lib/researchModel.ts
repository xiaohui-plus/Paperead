/**
 * 研究 agent 的当前模型。
 *
 * agent 进程只在启动时读一次供应商配置，所以这里把用户选中的模型同步给
 * 本地服务（/api/research/model），由 LLM 代理在转发时注入真实的
 * baseURL / API Key / 模型名。切换只影响后续消息，不需要重启 agent。
 */

import {
  isModelReady,
  loadModelSettings,
  readyModel,
  type ModelProfile,
} from './settings'

const STORAGE_KEY = 'paperead.researchModel'
const MODEL_API = '/api/research/model'

/** 设置里所有填好 Key 的回答模型。 */
export function listResearchModels(): ModelProfile[] {
  return loadModelSettings().models.filter(
    (model) => model.kind === 'answer' && isModelReady(model),
  )
}

/** 当前选中的研究模型：保存过就沿用，失效则退到可用的回答模型。 */
export function selectedResearchModel(): ModelProfile | null {
  const models = listResearchModels()
  let saved = ''
  try {
    saved = localStorage.getItem(STORAGE_KEY) ?? ''
  } catch {
    saved = ''
  }
  const hit = models.find((model) => model.id === saved)
  return hit ?? readyModel(loadModelSettings(), 'answer')
}

/** 保存选择并同步给本地服务。 */
export function setResearchModel(model: ModelProfile): void {
  try {
    localStorage.setItem(STORAGE_KEY, model.id)
  } catch {
    // 隐私模式下存不住，本次会话内仍可用
  }
  void syncResearchModel()
}

/** 把当前选择推给本地服务；失败静默，等下次切换再同步。 */
export async function syncResearchModel(): Promise<void> {
  const model = selectedResearchModel()
  if (!model) return
  try {
    await fetch(MODEL_API, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        base_url: model.baseUrl,
        api_key: model.apiKey,
        model: model.model,
        label: model.label,
      }),
    })
  } catch {
    // 服务未就绪时不打扰用户
  }
}
export interface ResearchModelStatus {
  base_url: string
  api_key: string
  model: string
  label: string
  configured: boolean
}

export async function researchModelStatus(): Promise<ResearchModelStatus | null> {
  try {
    const response = await fetch(MODEL_API, { cache: 'no-store' })
    if (!response.ok) return null
    return (await response.json()) as ResearchModelStatus
  } catch {
    return null
  }
}

/**
 * 发送前确认本地 LLM 代理真的拿到了模型配置。
 * 启动时的那次同步可能早于 sidecar 就绪而被静默丢弃，这里重推一次再复检。
 */
export async function ensureResearchModel(): Promise<string | null> {
  if (!selectedResearchModel()) {
    return '没有可用的回答模型：请到 设置 → 配置模型 填写 API Key 后再试。'
  }
  let status = await researchModelStatus()
  if (status && !status.configured) {
    await syncResearchModel()
    status = await researchModelStatus()
  }
  if (status && !status.configured) {
    return '研究模型没有同步到本地服务：请到 设置 → 配置模型 重新保存一次回答模型。'
  }
  return null
}
