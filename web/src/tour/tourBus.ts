export type TourPayload = Record<string, unknown>

type Handler = (payload: TourPayload) => void

const handlers = new Map<string, Set<Handler>>()

/** 极轻量的页面内事件总线：业务成功点发事件，教程只订阅当前步骤。 */
export const tourBus = {
  on(event: string, handler: Handler): () => void {
    const set = handlers.get(event) ?? new Set<Handler>()
    set.add(handler)
    handlers.set(event, set)
    return () => {
      set.delete(handler)
      if (set.size === 0) handlers.delete(event)
    }
  },

  emit(event: string, payload: TourPayload = {}): void {
    handlers.get(event)?.forEach((handler) => {
      try {
        handler(payload)
      } catch (error) {
        console.warn('[tour] handler failed', event, error)
      }
    })
  },
}
