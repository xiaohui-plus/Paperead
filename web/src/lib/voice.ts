/**
 * 浏览器语音识别（Web Speech API）的最小封装。
 *
 * Chrome / Edge（含桌面端 WebView2）通常提供 webkitSpeechRecognition；
 * Firefox 与部分内嵌环境没有。调用方必须把「不支持 / 没权限」明确告诉用户，
 * 不能做成点了没反应的装饰按钮。
 */

interface SpeechAlternativeLike {
  transcript: string
}

interface SpeechResultLike {
  isFinal: boolean
  0?: SpeechAlternativeLike
}

interface SpeechResultListLike {
  length: number
  [index: number]: SpeechResultLike | undefined
}

interface SpeechResultEventLike {
  resultIndex: number
  results: SpeechResultListLike
}

interface SpeechErrorEventLike {
  error?: string
}

export interface VoiceRecognizer {
  start(): void
  stop(): void
  abort(): void
}

interface RecognitionLike extends VoiceRecognizer {
  lang: string
  continuous: boolean
  interimResults: boolean
  maxAlternatives: number
  onresult: ((event: SpeechResultEventLike) => void) | null
  onerror: ((event: SpeechErrorEventLike) => void) | null
  onend: (() => void) | null
}

interface RecognitionCtor {
  new (): RecognitionLike
}

function recognitionCtor(): RecognitionCtor | null {
  if (typeof window === 'undefined') return null
  const scope = window as unknown as {
    SpeechRecognition?: RecognitionCtor
    webkitSpeechRecognition?: RecognitionCtor
  }
  return scope.SpeechRecognition ?? scope.webkitSpeechRecognition ?? null
}

export function voiceSupported(): boolean {
  return recognitionCtor() !== null
}

export function voiceErrorMessage(code: string): string {
  switch (code) {
    case 'not-allowed':
    case 'service-not-allowed':
      return '没有麦克风权限，请在系统设置里允许 Paperead 使用麦克风后重试。'
    case 'no-speech':
      return '没有听到声音，请靠近麦克风再说一次。'
    case 'audio-capture':
      return '找不到可用的麦克风，请检查设备连接。'
    case 'network':
      return '语音识别服务连不上，请检查网络后重试。'
    case 'aborted':
      return ''
    default:
      return `语音输入失败（${code}）`
  }
}

export function createVoiceRecognizer(handlers: {
  onText: (text: string, final: boolean) => void
  onError: (message: string) => void
  onEnd: () => void
}): VoiceRecognizer | null {
  const Ctor = recognitionCtor()
  if (!Ctor) return null

  const recognizer = new Ctor()
  recognizer.lang = 'zh-CN'
  recognizer.continuous = true
  recognizer.interimResults = true
  recognizer.maxAlternatives = 1

  recognizer.onresult = (event) => {
    let text = ''
    let final = true
    for (let i = 0; i < event.results.length; i += 1) {
      const result = event.results[i]
      if (!result) continue
      text += result[0]?.transcript ?? ''
      if (!result.isFinal) final = false
    }
    handlers.onText(text, final)
  }

  recognizer.onerror = (event) => {
    const message = voiceErrorMessage(event.error || 'unknown')
    if (message) handlers.onError(message)
  }

  recognizer.onend = () => handlers.onEnd()

  return {
    start: () => recognizer.start(),
    stop: () => recognizer.stop(),
    abort: () => recognizer.abort(),
  }
}