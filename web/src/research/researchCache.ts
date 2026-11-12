/**
 * 研究页的进程内快照缓存。
 *
 * 切换会话或切到文献库/阅读页时组件会卸载，但后端 agent 仍在跑、事件仍在落库。
 * 再次进入时先用上次快照立刻渲染，再从已记录的 seq 增量补齐，避免每次重新
 * 等待整段历史回放。缓存只存在于当前页面进程内，刷新页面后仍走完整回放。
 */

import type {
  ArtifactSummary,
  ProjectFile,
  ProjectSummary,
  SessionSummary,
} from '../lib/researchApi'
import type { ChatState } from '../lib/researchTypes'

export interface ProjectSnapshot {
  project: ProjectSummary | null
  files: ProjectFile[]
  sessions: SessionSummary[]
  artifacts: ArtifactSummary[]
}

export interface SessionSnapshot {
  chat: ChatState
  cursor: number
}

const PROJECT_LIMIT = 6
const SESSION_LIMIT = 12

const projects = new Map<string, ProjectSnapshot>()
const sessions = new Map<string, SessionSnapshot>()

function touch<T>(store: Map<string, T>, key: string, value: T, limit: number) {
  store.delete(key)
  store.set(key, value)
  while (store.size > limit) {
    const oldest = store.keys().next().value
    if (oldest === undefined) break
    store.delete(oldest)
  }
}

export function readProjectCache(projectId: string): ProjectSnapshot | null {
  return projects.get(projectId) ?? null
}

export function writeProjectCache(
  projectId: string,
  snapshot: ProjectSnapshot,
): void {
  touch(projects, projectId, snapshot, PROJECT_LIMIT)
}

export function dropProjectCache(projectId: string): void {
  projects.delete(projectId)
}

export function readSessionCache(sessionId: string): SessionSnapshot | null {
  return sessions.get(sessionId) ?? null
}

export function writeSessionCache(
  sessionId: string,
  chat: ChatState,
  cursor: number,
): void {
  touch(
    sessions,
    sessionId,
    { chat, cursor: Math.max(cursor, chat.lastSeq) },
    SESSION_LIMIT,
  )
}

export function dropSessionCache(sessionId: string): void {
  sessions.delete(sessionId)
}