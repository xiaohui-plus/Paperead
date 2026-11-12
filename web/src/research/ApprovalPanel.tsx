import { useState } from 'react'
import { IconShield } from '../components/icons'
import type { PendingApproval } from '../lib/researchTypes'

interface Props {
  approval: PendingApproval
  /** 触发审批的工具参数（终端命令等），用于让用户看清将执行什么。 */
  command?: string
  onAnswer: (outcome: 'allowed-once' | 'rejected') => Promise<void>
}

/** 一次性的权限申请：等用户回答后，agent 侧阻塞中的工具调用才继续。 */
export function ApprovalPanel({ approval, command, onAnswer }: Props) {
  const [answered, setAnswered] = useState(false)
  const [error, setError] = useState('')

  const answer = (outcome: 'allowed-once' | 'rejected') => {
    setAnswered(true)
    setError('')
    void onAnswer(outcome).catch((err) => {
      setAnswered(false)
      setError(err instanceof Error ? err.message : '提交失败，请重试')
    })
  }

  return (
    <div className="approval">
      <div className="approval-card">
        <div className="approval-head">
          <span className="approval-icon">
            <IconShield />
          </span>
          <span>等待你的授权</span>
        </div>
        <p className="approval-reason">
          {approval.reason || `${approval.toolName || '工具'} 需要更高的访问权限`}
        </p>
        {command && <pre className="approval-command">{command}</pre>}
        {error && <p className="approval-error">{error}</p>}
        <div className="approval-actions">
          <button
            type="button"
            className="approval-btn"
            disabled={answered}
            onClick={() => answer('rejected')}
          >
            拒绝
          </button>
          <button
            type="button"
            className="approval-btn is-primary"
            disabled={answered}
            onClick={() => answer('allowed-once')}
          >
            允许一次
          </button>
        </div>
      </div>
    </div>
  )
}