import { useCallback, useEffect, useRef, useState } from 'react'
import {
  getOnboarding,
  lastTourStep,
  markTourPromptShown,
  postOnboarding,
  saveLastTourStep,
  tourPromptShown,
  type OnboardingState,
} from '../lib/onboarding'
import { isDesktopApp } from '../lib/update'
import { TourGate } from './TourGate'
import { TourOverlay } from './TourOverlay'
import { tourSteps } from './tourSteps'
import './tour.css'

/**
 * 桌面端新手教程入口。
 *
 * - 只有 Tauri 客户端才会挂载；网页端不会加载这个模块。
 * - 首次安装后的首次启动展示一次选择卡，选择结果写入后端数据目录。
 * - 教程中途退出不自动恢复，设置页提供「继续教程 / 重新观看」。
 */
export function TourRoot() {
  const [gateOpen, setGateOpen] = useState(false)
  const [tourOpen, setTourOpen] = useState(false)
  const [startIndex, setStartIndex] = useState(0)
  const stateRef = useRef<OnboardingState | null>(null)
  const checkedRef = useRef(false)

  useEffect(() => {
    if (!isDesktopApp() || checkedRef.current) return
    checkedRef.current = true

    void (async () => {
      try {
        const state = await getOnboarding()
        stateRef.current = state
        if (state.shouldPrompt) {
          if (tourPromptShown()) {
            // 本地已经弹过，把状态补写到后端，避免下次再判断
            void postOnboarding('shown')
            return
          }
          markTourPromptShown()
          window.setTimeout(() => setGateOpen(true), 420)
          void postOnboarding('shown')
          return
        }
        if (state.legacyUser && !state.prompted) {
          void postOnboarding('legacy')
        }
      } catch (error) {
        // 后端不可用时不要打扰用户；教程仍可从设置手动打开
        console.warn('[tour] onboarding state unavailable', error)
      }
    })()
  }, [])

  useEffect(() => {
    const onStart = (event: Event) => {
      const detail = (event as CustomEvent<{ resume?: boolean }>).detail
      const last = detail?.resume
        ? (stateRef.current?.stepId ?? lastTourStep())
        : null
      const found = last
        ? tourSteps.findIndex((step) => step.id === last)
        : -1
      const index = found >= 0 ? found : 0
      setGateOpen(false)
      setStartIndex(index)
      setTourOpen(true)
      saveLastTourStep(tourSteps[index].id)
      void postOnboarding('start', tourSteps[index].id)
    }
    const onStop = () => {
      setGateOpen(false)
      setTourOpen(false)
      saveLastTourStep(null)
    }
    window.addEventListener('paperead:tour-start', onStart)
    window.addEventListener('paperead:tour-stop', onStop)
    return () => {
      window.removeEventListener('paperead:tour-start', onStart)
      window.removeEventListener('paperead:tour-stop', onStop)
    }
  }, [])

  const start = () => {
    markTourPromptShown()
    setGateOpen(false)
    setStartIndex(0)
    setTourOpen(true)
    saveLastTourStep(tourSteps[0].id)
    void postOnboarding('start', tourSteps[0].id)
  }

  const skip = () => {
    markTourPromptShown()
    setGateOpen(false)
    void postOnboarding('skip')
  }

  const progress = useCallback((stepId: string) => {
    saveLastTourStep(stepId)
    void postOnboarding('progress', stepId)
  }, [])

  const complete = useCallback(() => {
    saveLastTourStep(null)
    void postOnboarding('complete')
  }, [])

  if (!isDesktopApp()) return null

  return (
    <>
      {gateOpen && <TourGate onStart={start} onSkip={skip} />}
      {tourOpen && (
        <TourOverlay
          steps={tourSteps}
          startIndex={startIndex}
          onClose={() => setTourOpen(false)}
          onComplete={complete}
          onProgress={progress}
        />
      )}
    </>
  )
}
