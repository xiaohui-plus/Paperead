/**
 * 手机本机翻译：服务器不可达时，按页把 blocks 交给本机直连的模型流式翻译。
 *
 * 和电脑端保持一致：逐段出结果，先到的段落先显示；译完一页写回离线库，
 * 下次打开不用再翻。
 */
import type { Block, Paper } from '../types'
import { langLabel } from './translation'
import { streamLocalChat, type LocalLlmConfig } from './localLlm'

const SYSTEM = (target: string) =>
  `你是学术论文翻译专家，负责把英文学术论文翻译成${target}。要求：
1. 只输出译文正文，不要任何解释、前言、引号或 Markdown 代码块；
2. 学术书面语，忠实、准确、通顺，不增删内容，不概括；
3. 保持原文的段落、编号、列表结构；
4. 术语表未覆盖的专有名词保留英文原名。`

function buildPrompt(block: Block, target: string): string {
  const parts: string[] = []
  if (block.section) parts.push(`【当前章节】${block.section}`)
  parts.push(`【待翻译段落】\n${block.text}`)
  parts.push(`请只输出这一段的${target}译文。`)
  return parts.join('\n\n')
}

function clean(text: string): string {
  return text.replace(/^```[a-zA-Z]*\s*|\s*```$/g, '').trim()
}

async function pool<T>(
  items: T[],
  limit: number,
  worker: (item: T) => Promise<void>,
  signal?: AbortSignal,
): Promise<void> {
  let next = 0
  const runners = Array.from({ length: Math.min(limit, items.length) }, async () => {
    for (;;) {
      if (signal?.aborted) throw new DOMException('已停止翻译', 'AbortError')
      const index = next
      next += 1
      if (index >= items.length) return
      await worker(items[index])
    }
  })
  await Promise.all(runners)
}

export interface LocalTranslateOptions {
  paper: Paper
  pages: number[]
  config: LocalLlmConfig
  targetLang: string
  signal?: AbortSignal
  /** 流式增量：调用方直接更新这一段显示的译文 */
  onPartial: (blockId: string, text: string) => void
  /** 这一段翻译完成 */
  onBlock: (blockId: string, text: string) => void
  /** 整页完成，调用方可以写回离线库 */
  onPageDone?: (page: number, translations: Map<string, string>) => void | Promise<void>
}

/** 逐页、逐段流式翻译；失败时抛出，已完成的段落保留。 */
export async function translatePagesLocal(options: LocalTranslateOptions): Promise<void> {
  const target = langLabel(options.targetLang)
  for (const page of options.pages) {
    if (options.signal?.aborted) throw new DOMException('已停止翻译', 'AbortError')
    const blocks = options.paper.blocks.filter(
      (block) => block.page === page && block.translatable && block.text.trim() && !block.translated,
    )
    if (!blocks.length) continue

    const translations = new Map<string, string>()
    await pool(
      blocks,
      2,
      async (block) => {
        const messages = [
          { role: 'system' as const, content: SYSTEM(target) },
          { role: 'user' as const, content: buildPrompt(block, target) },
        ]
        const budget = Math.min(8000, Math.max(2048, Math.round(block.text.length * 1.2) + 1500))
        let text = ''
        try {
          const full = await streamLocalChat({
            config: options.config,
            messages,
            temperature: 0.25,
            maxTokens: budget,
            signal: options.signal,
            onDelta: (delta) => {
              text += delta
              options.onPartial(block.id, text)
            },
          })
          text = full
        } catch (error) {
          if (options.signal?.aborted) throw error
          // 清掉流到一半的内容，否则失败段落会永久显示残缺译文
          options.onPartial(block.id, '')
          // 单段失败不拖垮整页：下次进入这页还能重试
          return
        }
        const value = clean(text)
        if (!value) {
          options.onPartial(block.id, '')
          return
        }
        translations.set(block.id, value)
        options.onBlock(block.id, value)
      },
      options.signal,
    )

    if (translations.size) await options.onPageDone?.(page, translations)
  }
}
