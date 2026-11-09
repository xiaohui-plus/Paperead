import { useEffect, useRef, useState } from 'react'
import type { ModelProfile } from '../lib/settings'
import { IconCheck, IconSettings, IconSpark } from './icons'

interface Props {
  models: ModelProfile[]
  current: ModelProfile | null
  onChoose: (model: ModelProfile) => void
  onManage?: () => void
  /** 菜单向上展开，适合放在输入框底部。 */
  up?: boolean
}

/** 输入框里的模型切换器（照搬阅读页问答的选择器外观）。 */
export function ModelPicker({ models, current, onChoose, onManage, up }: Props) {
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onDown = (event: MouseEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [open])

  if (!current) {
    return (
      <button
        className="model-select is-empty"
        onClick={onManage}
        title={onManage ? '去设置里配置回答模型' : '还没有可用的回答模型'}
        disabled={!onManage}
      >
        <IconSpark /> 配置文件模型
      </button>
    )
  }

  return (
    <div className={`model-picker${up ? ' is-up' : ''}`} ref={root}>
      <button
        className="model-select"
        onClick={() => setOpen((value) => !value)}
        title="切换回答模型"
        aria-haspopup="menu"
        aria-expanded={open}
      >
        <span className="model-select-dot" />
        <span className="model-select-name">{current.label}</span>
        <span className="model-select-caret" aria-hidden>
          ▾
        </span>
      </button>
      {open && (
        <div className="model-menu" role="menu">
          <div className="model-menu-head">回答模型</div>
          {models.map((model) => (
            <button
              key={model.id}
              className="model-menu-item"
              data-active={model.id === current.id}
              role="menuitem"
              onClick={() => {
                onChoose(model)
                setOpen(false)
              }}
            >
              <span className="model-menu-main">
                <b>{model.label}</b>
                <code>{model.model}</code>
              </span>
              {model.id === current.id && <IconCheck />}
            </button>
          ))}
          {onManage && (
            <button
              className="model-menu-manage"
              onClick={() => {
                setOpen(false)
                onManage()
              }}
            >
              <IconSettings /> 管理模型…
            </button>
          )}
        </div>
      )}
    </div>
  )
}
