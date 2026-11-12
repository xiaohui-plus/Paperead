/** 记住荧光笔/批注各自上一次使用的颜色，直接点击图标时复用。 */

import type { AnnotationColor } from './library'

const HL_KEY = 'paperead.lastHlColor'
const CM_KEY = 'paperead.lastCommentColor'
const COLORS: AnnotationColor[] = ['yellow', 'green', 'blue', 'pink']

type Kind = 'highlight' | 'comment'

function keyOf(kind: Kind): string {
  return kind === 'highlight' ? HL_KEY : CM_KEY
}

export function lastAnnotationColor(kind: Kind): AnnotationColor {
  try {
    const raw = localStorage.getItem(keyOf(kind))
    if (raw && (COLORS as string[]).includes(raw)) return raw as AnnotationColor
  } catch {
    /* 隐私模式下读不到就用默认色 */
  }
  return kind === 'highlight' ? 'yellow' : 'blue'
}

export function rememberAnnotationColor(kind: Kind, color: AnnotationColor): void {
  try {
    localStorage.setItem(keyOf(kind), color)
  } catch {
    /* 写不进去只影响记忆，不影响当前标注 */
  }
}