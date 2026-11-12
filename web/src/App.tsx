import { useEffect, useState } from 'react'
import { LibraryPage } from './components/LibraryPage'
import { ReaderView } from './components/ReaderView'
import { ScholarPage } from './components/ScholarPage'
import type { NavKey } from './components/Sidebar'
import { MobileSetupScreen } from './components/MobileSetup'
import { SpikePage } from './research/SpikePage'
import { ResearchShell } from './research/ResearchShell'
import { ResearchHome } from './research/ResearchHome'
import { ProjectPage } from './research/ProjectPage'
import type { ProjectKind } from './lib/researchApi'
import { hasMobileServer, isMobileShell, loadOfflineMode } from './lib/mobile'

/**
 * 极简路由：用 query 参数而不是 hash，把 hash 留给 `#b0013` 这种块级深链。
 *   /                       → 文献库
 *   /?v=search&q=...        → 文献检索
 *   /?p=demo                → 阅读页
 *   /?view=research         → 研究首页
 *   /?view=review           → 写综述报告首页
 *   /?view=project&id=...   → 项目页（可带 sid / rename）
 *   /?research=spike        → A0 冒烟页（临时）
 * 阅读页优先于所有视图。
 */
type View = 'library' | 'search' | 'research' | 'review' | 'project'

interface Route {
  view: View
  projectId: string | null
  sessionId: string | null
  rename: boolean
}

function readRoute(): Route {
  const params = new URLSearchParams(window.location.search)
  const raw = params.get('view')
  const projectId = params.get('id')
  const sessionId = params.get('sid')
  const rename = params.get('rename') === '1'
  const mobile = isMobileShell()
  if (raw === 'project' && projectId && !mobile) {
    return { view: 'project', projectId, sessionId, rename }
  }
  if ((raw === 'research' || raw === 'review') && !mobile) {
    return { view: raw, projectId: null, sessionId: null, rename: false }
  }
  if (params.get('v') === 'search') {
    return { view: 'search', projectId: null, sessionId: null, rename: false }
  }
  return { view: 'library', projectId: null, sessionId: null, rename: false }
}

function currentPaperId(): string | null {
  return new URLSearchParams(window.location.search).get('p')
}

function currentSpike(): boolean {
  return new URLSearchParams(window.location.search).get('research') === 'spike'
}

export default function App() {
  const [paperId, setPaperId] = useState<string | null>(currentPaperId)
  const [spike, setSpike] = useState(currentSpike)
  const [route, setRoute] = useState<Route>(readRoute)
  // 首页发出的首条消息：不进 URL（刷新不应重发），只随本次导航传给会话页。
  const [initialText, setInitialText] = useState<string | null>(null)

  useEffect(() => {
    const onPop = () => {
      setPaperId(currentPaperId())
      setSpike(currentSpike())
      setRoute(readRoute())
      setInitialText(null)
    }
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [])

  // 站内页面（文献库/检索/研究/综述/项目）在窄屏取消 120% 缩放，阅读页保持原样。
  useEffect(() => {
    if (paperId) delete document.documentElement.dataset.surface
    else document.documentElement.dataset.surface = 'app'
  }, [paperId])

  const navigatePaper = (id: string | null) => {
    const url = new URL(window.location.href)
    if (id) url.searchParams.set('p', id)
    else url.searchParams.delete('p')
    if (!id) url.hash = ''
    window.history.pushState({}, '', url)
    setPaperId(id)
    setRoute(readRoute())
    setInitialText(null)
    if (id) window.scrollTo(0, 0)
  }

  const goView = (view: Exclude<View, 'project'>) => {
    // 安卓端已裁掉研究与综述，误入时统一回到文献库
    if (isMobileShell() && (view === 'research' || view === 'review')) view = 'library'
    const url = new URL(window.location.href)
    url.searchParams.delete('p')
    url.searchParams.delete('id')
    url.searchParams.delete('sid')
    url.searchParams.delete('rename')
    url.hash = ''
    if (view === 'search') {
      url.searchParams.set('v', 'search')
      url.searchParams.delete('view')
      url.searchParams.delete('pid')
    } else if (view === 'library') {
      url.searchParams.delete('v')
      url.searchParams.delete('view')
    } else {
      url.searchParams.set('view', view)
      url.searchParams.delete('v')
    }
    window.history.pushState({}, '', url)
    setPaperId(null)
    setRoute(readRoute())
    setInitialText(null)
  }

  const openProject = (
    projectId: string,
    options?: { rename?: boolean; sessionId?: string; initialText?: string },
  ) => {
    const url = new URL(window.location.href)
    url.searchParams.set('view', 'project')
    url.searchParams.set('id', projectId)
    url.searchParams.delete('v')
    url.searchParams.delete('p')
    url.hash = ''
    if (options?.sessionId) url.searchParams.set('sid', options.sessionId)
    else url.searchParams.delete('sid')
    if (options?.rename) url.searchParams.set('rename', '1')
    else url.searchParams.delete('rename')
    window.history.pushState({}, '', url)
    setPaperId(null)
    setRoute(readRoute())
    setInitialText(options?.initialText ?? null)
    window.scrollTo(0, 0)
  }

  const selectSession = (sessionId: string | null) => {
    const url = new URL(window.location.href)
    if (sessionId) url.searchParams.set('sid', sessionId)
    else url.searchParams.delete('sid')
    window.history.pushState({}, '', url)
    setRoute(readRoute())
    setInitialText(null)
  }

  const onNav = (key: NavKey) => {
    if (key === 'library') goView('library')
    else if (key === 'search') goView('search')
    else goView(key)
  }

  if (isMobileShell() && !hasMobileServer() && !loadOfflineMode()) {
    return <MobileSetupScreen />
  }

  if (paperId) {
    return <ReaderView key={paperId} paperId={paperId} onBack={() => navigatePaper(null)} />
  }

  if (spike) {
    return <SpikePage />
  }

  if (route.view === 'project' && route.projectId) {
    return (
      <ProjectPage
        key={route.projectId}
        projectId={route.projectId}
        sessionId={route.sessionId}
        initialRename={route.rename}
        initialText={initialText}
        onInitialTextConsumed={() => setInitialText(null)}
        onBack={(kind: ProjectKind) => goView(kind)}
        onSelectSession={selectSession}
      />
    )
  }

  if (route.view === 'research' || route.view === 'review') {
    const kind: ProjectKind = route.view
    return (
      <ResearchShell
        kind={kind}
        active={kind}
        onNav={onNav}
        onOpenProject={openProject}
      >
        {(projects, start) => (
          <ResearchHome kind={kind} projects={projects} onStart={start} />
        )}
      </ResearchShell>
    )
  }

  if (route.view === 'search') {
    return (
      <ScholarPage
        onOpenPaper={(id) => navigatePaper(id)}
        onNav={onNav}
      />
    )
  }

  return (
    <LibraryPage
      onOpenPaper={(id) => navigatePaper(id)}
      onNav={onNav}
    />
  )
}
