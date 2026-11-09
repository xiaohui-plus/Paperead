import { Component, type ErrorInfo, type ReactNode } from 'react'

interface Props {
  children: ReactNode
  title?: string
  hint?: string
}

interface State {
  error: Error | null
}

/** 渲染异常兜底：把整页白屏收窄成一块可重试的区域。 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('[render] 界面渲染失败', error, info.componentStack)
  }

  private reset = () => this.setState({ error: null })

  render() {
    const { error } = this.state
    if (!error) return this.props.children
    return (
      <div className="render-fallback" role="alert">
        <strong>{this.props.title || '这部分内容渲染失败'}</strong>
        <p>{this.props.hint || '已阻止整页白屏。可以重试，或切换到其他会话。'}</p>
        <pre>{error.message}</pre>
        <button type="button" onClick={this.reset}>
          重试
        </button>
      </div>
    )
  }
}
