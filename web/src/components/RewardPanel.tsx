import { useEffect, useMemo, useState } from 'react'
import {
  addRewardRecord,
  clearRewardRecords,
  formatRewardTime,
  loadRewardRecords,
  removeRewardRecord,
  rewardAmountText,
  type RewardChannel,
  type RewardRecord,
  type RewardTier,
} from '../lib/rewardHistory'
import { IconClose, IconCoffee, IconFlower, IconReward, IconStar, IconTrash } from './icons'
import alipayQr from '../assets/alipay-reward.jpg'
import giteeLogo from '../assets/gitee-logo.svg'
import githubLogo from '../assets/github-logo.svg'
import wechatQr from '../assets/wechat-reward.jpg'

type Tier = RewardTier
type Channel = RewardChannel

const GITHUB_REPO = 'https://github.com/xiaohui-plus/Paperead'
const GITEE_REPO = 'https://gitee.com/deng_xiaohui/paperead'
const RECENT_LIMIT = 5

const TIERS: { id: Tier; title: string; price: number; hint: string }[] = [
  {
    id: 'flower',
    title: '送一朵小花',
    price: 4.9,
    hint: '一杯奶茶的零头，够作者开心一整天。',
  },
  {
    id: 'coffee',
    title: '请作者喝杯咖啡',
    price: 9.9,
    hint: '熬夜改论文的时候，咖啡是刚需。',
  },
]

const TIER_TITLE: Record<Tier, string> = {
  flower: '送一朵小花',
  coffee: '请作者喝杯咖啡',
}

const CHANNEL_NAME: Record<Channel, string> = {
  wechat: '微信',
  alipay: '支付宝',
}

function tierIcon(tier: Tier, size = 18) {
  return tier === 'flower' ? <IconFlower size={size} /> : <IconCoffee size={size} />
}

/** 设置 → 打赏：先选档位，点支付再展示收款码；打赏记录只存本机。 */
export function RewardPanel() {
  const [tier, setTier] = useState<Tier | null>(null)
  const [channel, setChannel] = useState<Channel>('wechat')
  const [payOpen, setPayOpen] = useState(false)
  const [records, setRecords] = useState<RewardRecord[]>(loadRewardRecords)
  const [notice, setNotice] = useState('')
  const [confirmClear, setConfirmClear] = useState(false)
  const [showAll, setShowAll] = useState(false)

  const selected = TIERS.find((item) => item.id === tier) || null
  const total = useMemo(
    () => Math.round(records.reduce((sum, item) => sum + item.amount, 0) * 10) / 10,
    [records],
  )
  const visible = showAll ? records : records.slice(0, RECENT_LIMIT)

  useEffect(() => {
    if (!payOpen) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setPayOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [payOpen])

  const pickTier = (next: Tier) => {
    setTier(next)
    setNotice('')
    setConfirmClear(false)
  }

  const openPay = () => {
    if (!selected) return
    setPayOpen(true)
    setNotice('')
    setConfirmClear(false)
  }

  const confirmPaid = () => {
    if (!selected) return
    setRecords(
      addRewardRecord({ tier: selected.id, amount: selected.price, channel }),
    )
    setPayOpen(false)
    setShowAll(false)
    setNotice(`已记下这次支持：${selected.title} · ${rewardAmountText(selected.price)}`)
  }

  const clearHistory = () => {
    if (!confirmClear) {
      setConfirmClear(true)
      return
    }
    setRecords(clearRewardRecords())
    setConfirmClear(false)
    setShowAll(false)
    setNotice('打赏记录已清空。')
  }

  const qr = channel === 'wechat' ? wechatQr : alipayQr

  return (
    <div className="modal-body reward-panel">
      <section className="reward-hero">
        <div className="reward-hero-deco" aria-hidden>
          <span className="reward-deco-flower">
            <IconFlower size={22} />
          </span>
          <span className="reward-deco-heart">
            <IconReward size={16} />
          </span>
          <span className="reward-deco-coffee">
            <IconCoffee size={19} />
          </span>
        </div>
        <h3>如果 Paperead 陪你读过几个深夜</h3>
        <p>一颗星、一朵花、一杯咖啡，都是它继续长大的理由。</p>
      </section>

      <div className="reward-grid">
        <section className="reward-tile reward-star-tile" data-tone="star">
          <span className="reward-tile-top">
            <span className="reward-tile-icon">
              <IconStar size={20} />
            </span>
            <b>给仓库一颗星</b>
          </span>
          <span className="reward-tile-hint">免费，但对开源项目最有用。</span>
          <div className="reward-brand-links">
            <a
              className="reward-brand"
              href={GITHUB_REPO}
              target="_blank"
              rel="noreferrer"
              title="在 GitHub 给 Paperead 点 Star"
            >
              <img src={githubLogo} alt="" />
              GitHub
            </a>
            <a
              className="reward-brand"
              href={GITEE_REPO}
              target="_blank"
              rel="noreferrer"
              title="在 Gitee 给 Paperead 点 Star"
            >
              <img src={giteeLogo} alt="" />
              Gitee
            </a>
          </div>
        </section>

        {TIERS.map((item) => (
          <button
            key={item.id}
            type="button"
            className="reward-tile"
            data-tone={item.id}
            data-active={tier === item.id}
            aria-pressed={tier === item.id}
            onClick={() => pickTier(item.id)}
          >
            <span className="reward-tile-top">
              <span className="reward-tile-icon">{tierIcon(item.id, 20)}</span>
              <b>{item.title}</b>
            </span>
            <span className="reward-tile-hint">{item.hint}</span>
            <span className="reward-tile-price">{rewardAmountText(item.price)}</span>
          </button>
        ))}
      </div>

      <div className="reward-pay-bar" data-tone={selected?.id}>
        <div className="reward-pay-summary">
          {selected ? (
            <>
              <span className="reward-pay-icon" data-tone={selected.id}>
                {tierIcon(selected.id, 16)}
              </span>
              <b>{selected.title}</b>
              <span className="reward-pay-amount">{rewardAmountText(selected.price)}</span>
            </>
          ) : (
            <span className="reward-pay-empty">先选一朵小花或一杯咖啡</span>
          )}
        </div>
        <button
          type="button"
          className="btn primary reward-pay-btn"
          disabled={!selected}
          onClick={openPay}
        >
          {selected ? `去支付 ${rewardAmountText(selected.price)}` : '去支付'}
        </button>
      </div>

      {notice && <p className="reward-notice">{notice}</p>}

      <section className="reward-history">
        <div className="reward-history-head">
          <div>
            <b>我的打赏记录</b>
            <span>
              {records.length
                ? `共 ${records.length} 次 · ${rewardAmountText(total)}`
                : '只保存在这台设备上'}
            </span>
          </div>
          {records.length > 0 && (
            <button
              type="button"
              className="reward-clear"
              data-confirm={confirmClear}
              onClick={clearHistory}
            >
              {confirmClear ? '确认清空' : '清空'}
            </button>
          )}
        </div>

        {records.length === 0 ? (
          <p className="reward-history-empty">
            还没有记录。打赏后点一下「我已完成打赏」，会在这里留一笔。
          </p>
        ) : (
          <>
            <ul className="reward-history-list">
              {visible.map((item) => (
                <li className="reward-history-row" key={item.id}>
                  <span className="reward-history-icon" data-tone={item.tier}>
                    {tierIcon(item.tier, 15)}
                  </span>
                  <div className="reward-history-main">
                    <b>{TIER_TITLE[item.tier]}</b>
                    <span>
                      {CHANNEL_NAME[item.channel]} · {formatRewardTime(item.at)}
                    </span>
                  </div>
                  <span className="reward-history-amount">{rewardAmountText(item.amount)}</span>
                  <button
                    type="button"
                    className="reward-history-del"
                    title="删除这条记录"
                    aria-label="删除这条记录"
                    onClick={() => setRecords(removeRewardRecord(item.id))}
                  >
                    <IconTrash />
                  </button>
                </li>
              ))}
            </ul>
            {records.length > RECENT_LIMIT && (
              <button
                type="button"
                className="reward-history-more"
                onClick={() => setShowAll((value) => !value)}
              >
                {showAll ? '收起' : `查看全部 ${records.length} 条`}
              </button>
            )}
          </>
        )}
        <p className="reward-history-note">
          记录只用于自己回看，不代表支付校验，也不会同步到其他设备。
        </p>
      </section>

      <p className="reward-foot">
        打赏完全自愿，不影响任何功能；把论文读好、把研究做顺，才是 Paperead 最想帮到的事。
      </p>

      {payOpen && selected && (
        <div className="reward-sheet-scrim" onClick={() => setPayOpen(false)}>
          <div
            className="reward-sheet"
            data-tone={selected.id}
            role="dialog"
            aria-modal
            aria-label={`${selected.title} ${rewardAmountText(selected.price)}`}
            onClick={(event) => event.stopPropagation()}
          >
            <button
              type="button"
              className="reward-sheet-close"
              title="关闭"
              aria-label="关闭"
              onClick={() => setPayOpen(false)}
            >
              <IconClose />
            </button>

            <div className="reward-sheet-head">
              <span className="reward-sheet-icon" data-tone={selected.id}>
                {tierIcon(selected.id, 22)}
              </span>
              <div>
                <b>{selected.title}</b>
                <span>
                  扫码支付 <em>{rewardAmountText(selected.price)}</em>，金额可以自选
                </span>
              </div>
            </div>

            <div className="reward-channels" role="tablist" aria-label="收款方式">
              <button
                type="button"
                role="tab"
                aria-selected={channel === 'wechat'}
                data-active={channel === 'wechat'}
                onClick={() => setChannel('wechat')}
              >
                微信
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={channel === 'alipay'}
                data-active={channel === 'alipay'}
                onClick={() => setChannel('alipay')}
              >
                支付宝
              </button>
            </div>

            <figure className="reward-sheet-qr">
              <img src={qr} alt={`${CHANNEL_NAME[channel]}收款码`} />
            </figure>

            <p className="reward-sheet-tip">
              打开{CHANNEL_NAME[channel]}「扫一扫」，或长按图片识别二维码；付款备注里可以写一句话。
            </p>

            <div className="reward-sheet-actions">
              <button type="button" className="btn primary" onClick={confirmPaid}>
                我已完成打赏
              </button>
              <button type="button" className="btn ghost" onClick={() => setPayOpen(false)}>
                稍后再说
              </button>
            </div>

            <p className="reward-sheet-note">
              「我已完成打赏」只在本机记一笔，不会校验支付，也不会上传任何信息。
            </p>
          </div>
        </div>
      )}
    </div>
  )
}