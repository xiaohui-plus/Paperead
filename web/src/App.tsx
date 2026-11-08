import { useEffect, useState } from 'react'
import { LibraryPage } from './components/LibraryPage'
import { ReaderView } from './components/ReaderView'
import { ScholarPage } from './components/ScholarPage'
import { SpikePage } from './research/SpikePage'

/**
 * 极简路由：用 query 参数而不是 hash，把 hash 留给 `#b0013` 这种块级深链。
 *   /                  → 文献库
 *   /?v=search&q=...   → 文献检索
 *   /?p=demo           → 阅读页
 *   /?research=spike   → A0 研究 agent 冒烟页（临时）
 * 阅读页优先于视图：从检索页导入后会带着 `v=search` 进阅读页，返回时自然回到检索结果。
 */
type View = 'library' | 'search'

function currentPaperId(): string | null {
  return new URLSearchParams(window.location.search).get('p')
}

function currentView(): View {
  return new URLSearchParams(window.location.search).get('v') === 'search' ? 'search' : 'library'
}

function currentSpike(): boolean {
  return new URLSearchParams(window.location.search).get('research') === 'spike'
}

export default function App() {
  const [paperId, setPaperId] = useState<string | null>(currentPaperId)
  const [view, setView] = useState<View>(currentView)
  const [spike, setSpike] = useState(currentSpike)

  useEffect(() => {
    const onPop = () => {
      setPaperId(currentPaperId())
      setView(currentView())
      setSpike(currentSpike())
    }
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [])

  const navigate = (id: string | null) => {
    const url = new URL(window.location.href)
    if (id) url.searchParams.set('p', id)
    else url.searchParams.delete('p')
    if (!id) url.hash = ''
    window.history.pushState({}, '', url)
    setPaperId(id)
    // 从阅读页返回时保留 `v=search`，这样回到的是检索结果而不是文献库
    setView(currentView())
    if (id) window.scrollTo(0, 0)
  }

  const goView = (next: View) => {
    const url = new URL(window.location.href)
    if (next === 'search') {
      url.searchParams.set('v', 'search')
      // 从侧栏进检索页时回到结果列表，而不是上次停留的详情页
      url.searchParams.delete('pid')
    } else {
      url.searchParams.delete('v')
    }
    url.searchParams.delete('p')
    url.hash = ''
    window.history.pushState({}, '', url)
    setPaperId(null)
    setView(next)
  }

  if (paperId) {
    return <ReaderView key={paperId} paperId={paperId} onBack={() => navigate(null)} />
  }

  if (spike) {
    return <SpikePage />
  }

  if (view === 'search') {
    return (
      <ScholarPage
        onOpenPaper={(id) => navigate(id)}
        onNav={(key) => {
          if (key === 'library') goView('library')
        }}
      />
    )
  }

  return (
    <LibraryPage
      onOpenPaper={(id) => navigate(id)}
      onNav={(key) => {
        if (key === 'search') goView('search')
      }}
    />
  )
}
