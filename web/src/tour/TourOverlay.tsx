import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
} from 'react'
import { createPortal } from 'react-dom'
import { listPapers } from '../lib/library'
import { researchApi } from '../lib/researchApi'
import { tourBus } from './tourBus'
import type { TourPayload } from './tourBus'
import type { TourStep } from './tourSteps'

interface Props {
  steps: TourStep[]
  startIndex: number
  onClose: () => void
  onComplete: () => void
  onProgress: (stepId: string) => void
}

interface SpotRect {
  top: number
  left: number
  width: number
  height: number
}

type Side = 'right' | 'bottom' | 'left' | 'top'

const GAP = 14
const EDGE = 12
const TAB_LABEL: Record<string, string> = {
  outline: '目录',
  key: '重点',
  figures: '图表',
  lineage: '脉络',
  files: '文件',
  sessions: '会话',
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms))
}

function currentParam(name: string): string | null {
  return new URLSearchParams(window.location.search).get(name)
}

function isProjectRoute(): boolean {
  return (
    new URLSearchParams(window.location.search).get('view') === 'project' &&
    Boolean(currentParam('id'))
  )
}

function routeOf(target: string): URL {
  return new URL(target, window.location.href)
}

function navigate(target: string): void {
  const url = routeOf(target)
  if (url.href === window.location.href) return
  window.history.pushState({}, '', url)
  window.dispatchEvent(new PopStateEvent('popstate'))
  window.dispatchEvent(new Event('paperead:route'))
}

async function waitForElement(
  selector: string,
  timeout: number,
): Promise<HTMLElement | null> {
  const deadline = Date.now() + timeout
  while (Date.now() < deadline) {
    const element = document.querySelector<HTMLElement>(selector)
    if (element) return element
    await sleep(120)
  }
  return null
}

export function TourOverlay({
  steps,
  startIndex,
  onClose,
  onComplete,
  onProgress,
}: Props) {
  const [index, setIndex] = useState(
    Math.max(0, Math.min(startIndex, steps.length - 1)),
  )
  const [rect, setRect] = useState<SpotRect | null>(null)
  const [missing, setMissing] = useState(false)
  const [done, setDone] = useState(false)
  const [covered, setCovered] = useState(false)
  const [narrow, setNarrow] = useState(
    () => typeof window !== 'undefined' && window.innerWidth < 720,
  )
  const [bubblePos, setBubblePos] = useState<CSSProperties>({})
  const [side, setSide] = useState<Side>('right')
  const [visitedTabs, setVisitedTabs] = useState<Set<string>>(() => new Set())
  const [fallbackReady, setFallbackReady] = useState(false)

  const bubbleRef = useRef<HTMLDivElement>(null)
  const doneRef = useRef(false)
  const visitedRef = useRef<Set<string>>(new Set())
  const startedAtRef = useRef(Math.floor(Date.now() / 1000))
  const completeRef = useRef(false)
  const advanceTimer = useRef<number | null>(null)

  const step = steps[index]

  const clearAdvance = () => {
    if (advanceTimer.current !== null) {
      window.clearTimeout(advanceTimer.current)
      advanceTimer.current = null
    }
  }

  const handleDone = useCallback(() => {
    if (doneRef.current) return
    doneRef.current = true
    setDone(true)
    clearAdvance()
    advanceTimer.current = window.setTimeout(() => {
      doneRef.current = false
      advanceTimer.current = null
      setDone(false)
      setIndex((prev) => Math.min(prev + 1, steps.length - 1))
    }, 720)
  }, [steps.length])

  const prepareStep = useCallback(async (item: TourStep) => {
    if (item.route) {
      const target = routeOf(item.route)
      if (
        target.pathname + target.search !==
        window.location.pathname + window.location.search
      ) {
        navigate(target.pathname + target.search)
        await sleep(380)
      }
    }

    if (item.prepare === 'open-first-paper' && !currentParam('p')) {
      const papers = await listPapers()
      const paper = papers.find((row) => row.status === 'ready') ?? papers[0]
      if (!paper) throw new Error('还没有可阅读的论文')
      navigate(`/?p=${encodeURIComponent(paper.id)}`)
      await sleep(520)
    }

    if (item.prepare === 'open-latest-project' && !isProjectRoute()) {
      const deadline = Date.now() + 6000
      while (Date.now() < deadline) {
        const research = await researchApi.listProjects('research')
        const review = await researchApi.listProjects('review')
        const project = research[0] ?? review[0]
        if (project) {
          navigate(`/?view=project&id=${encodeURIComponent(project.id)}`)
          await sleep(520)
          return
        }
        await sleep(420)
      }
      throw new Error('还没有可打开的项目')
    }

    if (item.prepare === 'open-review-project' && !isProjectRoute()) {
      const since = startedAtRef.current - 15
      const deadline = Date.now() + 9000
      while (Date.now() < deadline) {
        const projects = await researchApi.listProjects('review')
        const fresh = projects.find((project) => project.createdAt >= since)
        if (fresh) {
          navigate(`/?view=project&id=${encodeURIComponent(fresh.id)}`)
          await sleep(520)
          return
        }
        await sleep(420)
      }
      const projects = await researchApi.listProjects('review')
      if (!projects[0]) throw new Error('旧大纲还没有生成出项目')
      navigate(`/?view=project&id=${encodeURIComponent(projects[0].id)}`)
      await sleep(520)
    }

    if (item.prepare === 'open-latest-review-project' && !isProjectRoute()) {
      const projects = await researchApi.listProjects('review')
      if (!projects[0]) throw new Error('还没有综述项目')
      navigate(`/?view=project&id=${encodeURIComponent(projects[0].id)}`)
      await sleep(520)
    }
  }, [])

  const locate = useCallback(async (selector: string, cancelled: () => boolean) => {
    const element = await waitForElement(selector, 5200)
    if (!element || cancelled()) {
      if (!cancelled()) setMissing(true)
      return
    }
    const first = element.getBoundingClientRect()
    const outside =
      first.top < 8 ||
      first.bottom > window.innerHeight - 8 ||
      first.left < 8 ||
      first.right > window.innerWidth - 8
    if (outside) {
      element.scrollIntoView({ block: 'center', inline: 'nearest' })
      await sleep(220)
    }
    if (cancelled()) return
    const box = element.getBoundingClientRect()
    setRect({
      top: box.top,
      left: box.left,
      width: box.width,
      height: box.height,
    })
    setMissing(false)
  }, [])

  // 当前步骤：重置状态、准备页面、等待锚点
  useEffect(() => {
    if (!step) return
    let cancelled = false
    clearAdvance()
    doneRef.current = false
    visitedRef.current = new Set()
    completeRef.current = step.id === 'done' ? completeRef.current : false
    setDone(false)
    setMissing(false)
    setRect(null)
    setVisitedTabs(new Set())
    setFallbackReady(false)
    startedAtRef.current = Math.floor(Date.now() / 1000)
    onProgress(step.id)

    if (!step.selector) return

    void (async () => {
      try {
        await prepareStep(step)
      } catch {
        // 准备失败时交给锚点等待逻辑展示兜底
      }
      if (!cancelled) await locate(step.selector, () => cancelled)
      if (!cancelled && step.ready?.()) handleDone()
    })()

    const fallbackTimer = window.setTimeout(() => setFallbackReady(true), 12000)
    return () => {
      cancelled = true
      window.clearTimeout(fallbackTimer)
    }
  }, [handleDone, index, locate, onProgress, prepareStep, step])

  // 滚动、缩放、路由变化时重新贴合锚点
  useEffect(() => {
    if (!step?.selector) return
    const measure = () => {
      const element = document.querySelector<HTMLElement>(step.selector)
      if (!element) return
      const box = element.getBoundingClientRect()
      setRect({
        top: box.top,
        left: box.left,
        width: box.width,
        height: box.height,
      })
      setMissing(false)
    }
    const onResize = () => {
      setNarrow(window.innerWidth < 720)
      measure()
    }
    window.addEventListener('resize', onResize)
    window.addEventListener('scroll', measure, true)
    window.addEventListener('paperead:route', measure)
    window.addEventListener('popstate', measure)
    const timer = window.setInterval(measure, 900)
    return () => {
      window.removeEventListener('resize', onResize)
      window.removeEventListener('scroll', measure, true)
      window.removeEventListener('paperead:route', measure)
      window.removeEventListener('popstate', measure)
      window.clearInterval(timer)
    }
  }, [step?.selector])

  // 设置页是全屏视图，打开时先收起教程，保存后再回来
  useEffect(() => {
    const check = () => setCovered(Boolean(document.querySelector('.settings-page')))
    check()
    const observer = new MutationObserver(check)
    observer.observe(document.body, { childList: true, subtree: true })
    return () => observer.disconnect()
  }, [])

  // 订阅当前步骤的完成事件
  useEffect(() => {
    if (!step?.event || step.id === 'done') return
    const events = Array.isArray(step.event) ? step.event : [step.event]
    const unsubs = events.map((event) =>
      tourBus.on(event, (payload: TourPayload) => {
        if (step.match && !step.match(payload)) return
        if (step.requiredTabs?.length) {
          const tab = String(payload.tab ?? '')
          if (tab) visitedRef.current.add(tab)
          setVisitedTabs(new Set(visitedRef.current))
          if (step.requiredTabs.every((item) => visitedRef.current.has(item))) {
            handleDone()
          }
          return
        }
        handleDone()
      }),
    )
    return () => unsubs.forEach((unsubscribe) => unsubscribe())
  }, [handleDone, step])

  // 到达完成卡时只上报一次
  useEffect(() => {
    if (step?.id !== 'done' || completeRef.current) return
    completeRef.current = true
    onComplete()
  }, [onComplete, step?.id])

  // 气泡定位：右侧 → 下方 → 左侧 → 上方，越界自动翻转
  useLayoutEffect(() => {
    if (!rect || narrow || missing) {
      setBubblePos({})
      setSide('bottom')
      return
    }
    const bubble = bubbleRef.current
    if (!bubble) return
    const width = bubble.offsetWidth
    const height = bubble.offsetHeight
    const viewportW = window.innerWidth
    const viewportH = window.innerHeight
    const preferred = step?.placement ?? 'right'
    const order: Side[] = [preferred, 'right', 'bottom', 'left', 'top']
    const seen = new Set<Side>()

    const clamp = (value: number, min: number, max: number) =>
      Math.max(min, Math.min(max, value))

    for (const candidate of order) {
      if (seen.has(candidate)) continue
      seen.add(candidate)
      if (candidate === 'right' && rect.left + rect.width + GAP + width <= viewportW - EDGE) {
        setSide('right')
        setBubblePos({
          left: Math.round(rect.left + rect.width + GAP),
          top: Math.round(
            clamp(rect.top + rect.height / 2 - height / 2, EDGE, viewportH - height - EDGE),
          ),
        })
        return
      }
      if (candidate === 'left' && rect.left - GAP - width >= EDGE) {
        setSide('left')
        setBubblePos({
          left: Math.round(rect.left - GAP - width),
          top: Math.round(
            clamp(rect.top + rect.height / 2 - height / 2, EDGE, viewportH - height - EDGE),
          ),
        })
        return
      }
      if (candidate === 'bottom' && rect.top + rect.height + GAP + height <= viewportH - EDGE) {
        setSide('bottom')
        setBubblePos({
          left: Math.round(
            clamp(rect.left + rect.width / 2 - width / 2, EDGE, viewportW - width - EDGE),
          ),
          top: Math.round(rect.top + rect.height + GAP),
        })
        return
      }
      if (candidate === 'top' && rect.top - GAP - height >= EDGE) {
        setSide('top')
        setBubblePos({
          left: Math.round(
            clamp(rect.left + rect.width / 2 - width / 2, EDGE, viewportW - width - EDGE),
          ),
          top: Math.round(rect.top - GAP - height),
        })
        return
      }
    }

    setSide('bottom')
    setBubblePos({
      left: EDGE,
      top: Math.round(clamp(rect.top + rect.height + GAP, EDGE, viewportH - height - EDGE)),
    })
  }, [rect, narrow, missing, step, done, visitedTabs, fallbackReady])

  // Esc 退出；不拦截其它按键，避免影响正常操作
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  useEffect(() => () => clearAdvance(), [])

  if (!step || covered) return null

  if (step.id === 'done') {
    return createPortal(
      <div className="tour-layer">
        <div className="tour-complete">
          <span className="tour-complete-mark">✓</span>
          <h2>第一遍走完了</h2>
          <p>{step.body}</p>
          <ul className="tour-complete-list">
            <li>读一篇论文：导读、问答、目录与图表</li>
            <li>找一次文献：检索、筛选、加入文献库</li>
            <li>做一个研究项目：附件、会话、产物</li>
            <li>写一篇综述：大纲、初稿、引用审计与导出</li>
          </ul>
          <div className="tour-complete-actions">
            <button
              className="tour-btn primary"
              onClick={() => {
                navigate('?view=research')
                onClose()
              }}
            >
              去研究
            </button>
            <button
              className="tour-btn ghost"
              onClick={() => {
                completeRef.current = false
                setIndex(0)
              }}
            >
              再走一遍
            </button>
            <button className="tour-link" onClick={onClose}>
              关闭
            </button>
          </div>
        </div>
      </div>,
      document.body,
    )
  }

  const eventStep = Boolean(step.event)
  const primaryDisabled = !done && eventStep && !fallbackReady
  const visitedCount = step.requiredTabs
    ? step.requiredTabs.filter((tab) => visitedTabs.has(tab)).length
    : 0

  return createPortal(
    <div className="tour-layer" role="dialog" aria-modal={false} aria-label="新手教程">
      {rect && !missing && (
        <div
          className="tour-spot"
          style={{
            top: rect.top - 6,
            left: rect.left - 6,
            width: rect.width + 12,
            height: rect.height + 12,
          }}
        />
      )}
      <div
        ref={bubbleRef}
        className="tour-bubble"
        data-side={side}
        data-center={missing || !rect ? 'true' : 'false'}
        style={narrow || missing ? undefined : bubblePos}
        aria-live="polite"
      >
        <button className="tour-close" onClick={onClose} title="退出教程" aria-label="退出教程">
          ×
        </button>
        <div className="tour-bubble-head">
          <span className="tour-chip">
            第 {index + 1} 步 · {step.chapter}
          </span>
          <span className="tour-count">
            {index + 1} / {steps.length}
          </span>
        </div>
        <div className="tour-progress">
          <i style={{ width: `${((index + 1) / steps.length) * 100}%` }} />
        </div>
        <h3>{step.title}</h3>
        <p>{step.body}</p>
        <div className="tour-action">
          <span className="tour-action-dot" />
          <span>{step.action}</span>
        </div>
        {step.requiredTabs?.length ? (
          <div className="tour-check-list">
            {step.requiredTabs.map((tab) => (
              <span
                key={tab}
                className="tour-check-item"
                data-done={visitedTabs.has(tab)}
              >
                <i>{visitedTabs.has(tab) ? '✓' : '·'}</i>
                {TAB_LABEL[tab] ?? tab}
              </span>
            ))}
            <span className="tour-check-count">
              {visitedCount}/{step.requiredTabs.length}
            </span>
          </div>
        ) : null}
        {missing ? (
          <p className="tour-missing">
            入口还没出现。如果论文正在解析或页面正在加载，完成后会自动定位；也可以跳过这一步。
          </p>
        ) : null}
        <div className="tour-bubble-foot">
          <div>
            {index > 0 && (
              <button
                className="tour-link"
                onClick={() => setIndex((prev) => Math.max(0, prev - 1))}
              >
                上一步
              </button>
            )}
            {!done && (
              <button
                className="tour-link"
                onClick={() => setIndex((prev) => Math.min(prev + 1, steps.length - 1))}
              >
                跳过这一步
              </button>
            )}
          </div>
          <button
            className="tour-btn primary"
            disabled={primaryDisabled}
            onClick={handleDone}
          >
            {done
              ? '✓ 已完成'
              : eventStep
                ? fallbackReady
                  ? '我已完成 →'
                  : '等待完成…'
                : '我已完成 →'}
          </button>
        </div>
        {eventStep && !done ? (
          <p className="tour-footnote">完成上面的操作后会自动进入下一步</p>
        ) : null}
      </div>
    </div>,
    document.body,
  )
}
