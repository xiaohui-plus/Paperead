import { useCallback, useEffect, useState } from 'react'
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

interface Props {
  kind: ProjectKind
  active: 'research' | 'review'
  onNav: (key: NavKey) => void
  onOpenProject: (
    projectId: string,
    options?: { rename?: boolean; sessionId?: string; initialText?: string },
  ) => void
  children: (projects: ProjectSummary[], start: StartHandler) => ReactNode
}

export type StartHandler = (
  text: string,
  title: string,
  projectId: string | null,
) => Promise<void>

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : '项目加载失败'
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
  const [starting, setStarting] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setProjects(await researchApi.listProjects(kind))
    } catch (loadError) {
      setError(errorText(loadError))
    } finally {
      setLoading(false)
    }
  }, [kind])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    if (kind === 'research') tourBus.emit('research:opened')
  }, [kind])

  const createProject = async (name?: string): Promise<ProjectSummary | null> => {
    setCreating(true)
    setError(null)
    try {
      const project = await researchApi.createProject(kind, name)
      setProjects((prev) => [project, ...prev])
      return project
    } catch (createError) {
      setError(errorText(createError))
      return null
    } finally {
      setCreating(false)
    }
  }

  const handleCreate = async () => {
    const project = await createProject()
    if (project) onOpenProject(project.id, { rename: true })
  }

  const start: StartHandler = async (text, title, projectId) => {
    if (starting) return
    setStarting(true)
    try {
      const sessionTitle = title || text.slice(0, 40) || '新会话'
      let target = projectId
      if (!target) {
        // 新项目直接用首条消息命名，避免侧栏里出现一排“新研究/新综述”。
        const created = await createProject(sessionTitle)
        if (!created) return
        target = created.id
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
            onCreate={() => void handleCreate()}
            onOpen={(project) => onOpenProject(project.id)}
            onRetry={() => void load()}
          />
        }
      />
      <main className="library-main research-main">
        {children(projects, start)}
      </main>
      <SettingsDialog open={settingsOpen} onClose={() => setSettingsOpen(false)} />
    </div>
  )
}
