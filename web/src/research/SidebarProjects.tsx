import type { ProjectKind, ProjectSummary } from '../lib/researchApi'
import { IconFolder, IconPlus } from '../components/icons'

interface Props {
  kind: ProjectKind
  projects: ProjectSummary[]
  activeId?: string | null
  loading: boolean
  error: string | null
  creating: boolean
  onCreate: () => void
  onOpen: (project: ProjectSummary) => void
  onRetry: () => void
}

/** 侧栏项目分区：只列当前功能的项目，绝不列会话。 */
export function SidebarProjects({
  kind,
  projects,
  activeId,
  loading,
  error,
  creating,
  onCreate,
  onOpen,
  onRetry,
}: Props) {
  return (
    <div className="library-folders project-section" data-kind={kind}>
      <div className="folders-head">
        <span>项目</span>
        <button
          className="icon-btn"
          title="新建项目"
          disabled={creating}
          onClick={onCreate}
        >
          <IconPlus />
        </button>
      </div>

      {loading ? (
        <div className="project-skeleton" aria-label="正在加载项目">
          <span />
          <span />
          <span />
        </div>
      ) : error ? (
        <div className="folders-empty">
          <p>{error}</p>
          <button className="project-retry" onClick={onRetry}>
            重试
          </button>
        </div>
      ) : projects.length === 0 ? (
        <div className="folders-empty">
          <p>还没有项目</p>
          <button className="project-empty-create" onClick={onCreate} disabled={creating}>
            <IconPlus />
            新建项目
          </button>
        </div>
      ) : (
        <div className="project-list">
          {projects.map((project) => (
            <button
              key={project.id}
              className="project-row"
              data-active={project.id === activeId}
              title={project.name}
              onClick={() => onOpen(project)}
            >
              <IconFolder />
              <span className="project-row-name">{project.name}</span>
              {project.running && <i className="project-running" title="有会话正在运行" />}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
