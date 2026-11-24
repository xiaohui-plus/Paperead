import { useCallback, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { Sidebar, type NavKey } from '../components/Sidebar'
import { SettingsDialog } from '../components/SettingsDialog'
import {
  researchApi,
  type ProjectKind,
  type ProjectSummary,
  type SessionSummary,
} from '../lib/researchApi'
import { tourBus } from '../tour/tourBus'
import { SidebarProjects } from './SidebarProjects'
import { CreateProjectDialog } from './CreateProjectDialog'

interface Props {
  kind: ProjectKind
  active: 'research' | 'review'
  onNav: (key: NavKey) => void
  onOpenProject: (
    projectId: string,
    options?: { rename?: boolean; sessionId?: string; initialText?: string },
  ) => void
  children: (
    projects: ProjectSummary[],
    start: StartHandler,
    createProject: CreateProjectHandler,
    openSettings: () => void,
  ) => ReactNode
}

export type StartHandler = (
  text: string,
  title: string,
  projectId: string | null,
  /** 本次选择的工作文件夹；空字符串表示沿用项目自己的文件夹。 */
  rootPath?: string,
) => Promise<void>

/** 首页「选择项目 → 创建项目」用的快速创建入口，不跳转页面。 */
export type CreateProjectHandler = (
  name: string,
  rootPath: string,
) => Promise<{
  ok: boolean
  project?: ProjectSummary
  error?: string
}>

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : '项目加载失败'
}

/** sidecar 先起、研究 agent 在窗口打开后后台启动：这段时间研究接口是 503。 */
function isAgentStarting(message: string): boolean {
  return (
    message.includes('研究 agent 尚未启动') || message.includes('研究 agent 不可用')
  )
}

/** 研究与写综述共用的页面外壳：全局侧栏 + 本功能的项目分区。 */
export function ResearchShell({
  kind,
  active,
  onNav,
  onOpenProject,
  children,
}: Props) {
  const [projects, setProjects] = useState<ProjectSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const [createOpen, setCreateOpen] = useState(false)
  const [starting, setStarting] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)

  const retryTimer = useRef<number | undefined>(undefined)
  const agentRetry = useRef(0)
  const loadRef = useRef<(options?: { silent?: boolean }) => Promise<void>>(
    async () => {},
  )

  const load = useCallback(
    async (options?: { silent?: boolean }) => {
      const silent = options?.silent === true
      if (retryTimer.current !== undefined) {
        window.clearTimeout(retryTimer.current)
        retryTimer.current = undefined
      }
      if (!silent) {
        setLoading(true)
        setError(null)
      }
      try {
        setProjects(await researchApi.listProjects(kind))
        setError(null)
        agentRetry.current = 0
      } catch (loadError) {
        const message = errorText(loadError)
        setError(message)
        // agent 还在后台启动时自动退避补拉，用户不必手动点「重试」。
        if (isAgentStarting(message) && agentRetry.current < 4) {
          const delay = 1000 * 2 ** agentRetry.current
          agentRetry.current += 1
          retryTimer.current = window.setTimeout(() => {
            void loadRef.current({ silent: true })
          }, delay)
        }
      } finally {
        if (!silent) setLoading(false)
      }
    },
    [kind],
  )
  loadRef.current = load

  useEffect(() => {
    void load()
  }, [load])

  useEffect(
    () => () => {
      if (retryTimer.current !== undefined) window.clearTimeout(retryTimer.current)
    },
    [],
  )

  useEffect(() => {
    if (kind === 'research') tourBus.emit('research:opened')
  }, [kind])

  const createProject = async (
    name?: string,
    rootPath?: string,
  ): Promise<{ project: ProjectSummary | null; error: string }> => {
    setCreating(true)
    setError(null)
    try {
      const project = await researchApi.createProject(kind, name, rootPath)
      setProjects((prev) => [project, ...prev])
      return { project, error: '' }
    } catch (createError) {
      const message = errorText(createError)
      setError(message)
      return { project: null, error: message }
    } finally {
      setCreating(false)
    }
  }

  const handleCreate = () => setCreateOpen(true)

  const finishCreate = async (
    name: string,
    rootPath: string,
  ): Promise<{ ok: boolean; error?: string }> => {
    const result = await createProject(name, rootPath)
    if (!result.project) return { ok: false, error: result.error }
    setCreateOpen(false)
    onOpenProject(result.project.id)
    return { ok: true }
  }

  const start: StartHandler = async (text, title, projectId, rootPath = '') => {
    if (starting) return
    setStarting(true)
    try {
      const sessionTitle = title || text.slice(0, 40) || '新会话'
      let target = projectId
      if (!target) {
        // 新项目直接用首条消息命名，避免侧栏里出现一排“新研究/新综述”。
        const created = await createProject(sessionTitle, rootPath)
        if (!created.project) return
        target = created.project.id
      } else if (rootPath) {
        // 已有项目换了目录：先落盘根目录，后续会话与 agent 都在这里工作。
        const updated = await researchApi.setProjectRoot(target, rootPath)
        setProjects((prev) =>
          prev.map((item) => (item.id === updated.id ? updated : item)),
        )
      }
      const session: SessionSummary = await researchApi.createSession(
        target,
        sessionTitle,
      )
      // 首条消息随导航一起带进会话页：只建会话不发消息，用户输入会被丢掉。
      onOpenProject(target, { sessionId: session.id, initialText: text })
    } catch (startError) {
      setError(errorText(startError))
    } finally {
      setStarting(false)
    }
  }

  return (
    <div className="library research-shell">
      <Sidebar
        active={active}
        onNav={onNav}
        onSettings={() => setSettingsOpen(true)}
        projectSection={
          <SidebarProjects
            kind={kind}
            projects={projects}
            loading={loading}
            error={error}
            creating={creating}
            onCreate={handleCreate}
            onOpen={(project) => onOpenProject(project.id)}
            onRetry={() => void load()}
          />
        }
      />
      <main className="library-main research-main">
        {children(projects, start, async (name, rootPath) => {
          const result = await createProject(name, rootPath)
          return {
            ok: Boolean(result.project),
            project: result.project ?? undefined,
            error: result.error,
          }
        }, () => setSettingsOpen(true))}
      </main>
      <SettingsDialog open={settingsOpen} onClose={() => setSettingsOpen(false)} />
      <CreateProjectDialog
        open={createOpen}
        busy={creating}
        onClose={() => setCreateOpen(false)}
        onCreate={finishCreate}
      />
    </div>
  )
}
