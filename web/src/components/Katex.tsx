import { useEffect, useRef, useState } from 'react'
import katex from 'katex'

interface Props {
  tex: string
  display?: boolean
  onError?: () => void
}

/** KaTeX 渲染。渲染失败（不支持的宏等）时回调给上层，由上层回落到图片。 */
export function Katex({ tex, display = true, onError }: Props) {
  const ref = useRef<HTMLSpanElement>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    const el = ref.current
    if (!el) return
    try {
      katex.render(tex, el, {
        displayMode: display,
        throwOnError: true,
        strict: false,
        trust: false,
      })
      setFailed(false)
    } catch {
      setFailed(true)
      onError?.()
    }
  }, [tex, display, onError])

  if (failed) return null
  return <span ref={ref} className="katex-host" />
}
