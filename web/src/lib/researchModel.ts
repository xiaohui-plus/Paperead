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
