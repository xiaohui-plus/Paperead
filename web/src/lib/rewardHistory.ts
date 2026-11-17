/** 本机打赏记录：只写入 localStorage，不上传、不做支付校验。 */

export type RewardTier = 'flower' | 'coffee'
export type RewardChannel = 'wechat' | 'alipay'

export interface RewardRecord {
  id: string
  tier: RewardTier
  amount: number
  channel: RewardChannel
  at: number
}

const KEY = 'paperead.rewardHistory.v1'
const MAX_RECORDS = 50

function isTier(value: unknown): value is RewardTier {
  return value === 'flower' || value === 'coffee'
}

function isChannel(value: unknown): value is RewardChannel {
  return value === 'wechat' || value === 'alipay'
}

function normalize(raw: unknown): RewardRecord | null {
  if (!raw || typeof raw !== 'object') return null
  const item = raw as Partial<RewardRecord>
  if (!isTier(item.tier) || !isChannel(item.channel)) return null
  const amount = Number(item.amount)
  const at = Number(item.at)
  if (!Number.isFinite(amount) || amount <= 0 || !Number.isFinite(at) || at <= 0) return null
  return {
    id: String(item.id || `${at}-${Math.random().toString(36).slice(2, 8)}`),
    tier: item.tier,
    amount,
    channel: item.channel,
    at,
  }
}

function save(records: RewardRecord[]): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(records.slice(0, MAX_RECORDS)))
  } catch {
    /* 写不进去只影响记录，不影响打赏流程 */
  }
}

export function loadRewardRecords(): RewardRecord[] {
  try {
    const raw = localStorage.getItem(KEY)
    if (!raw) return []
    const list = JSON.parse(raw)
    if (!Array.isArray(list)) return []
    return list
      .map(normalize)
      .filter((item): item is RewardRecord => Boolean(item))
      .sort((a, b) => b.at - a.at)
      .slice(0, MAX_RECORDS)
  } catch {
    return []
  }
}

export function addRewardRecord(input: {
  tier: RewardTier
  amount: number
  channel: RewardChannel
}): RewardRecord[] {
  const record: RewardRecord = {
    id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    tier: input.tier,
    amount: input.amount,
    channel: input.channel,
    at: Date.now(),
  }
  const next = [record, ...loadRewardRecords()].slice(0, MAX_RECORDS)
  save(next)
  return next
}

export function removeRewardRecord(id: string): RewardRecord[] {
  const next = loadRewardRecords().filter((item) => item.id !== id)
  save(next)
  return next
}

export function clearRewardRecords(): RewardRecord[] {
  save([])
  return []
}

export function formatRewardTime(at: number): string {
  try {
    return new Date(at).toLocaleString('zh-CN', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    })
  } catch {
    return '刚刚'
  }
}

export function rewardAmountText(amount: number): string {
  return `¥${Number.isInteger(amount) ? amount : amount.toFixed(1)}`
}