import { useEffect, useState } from 'react'
import { LibraryPage } from './components/LibraryPage'
import { ReaderView } from './components/ReaderView'

/**
 * 极简路由：用 query 参数而不是 hash，把 hash 留给 `#b0013` 这种块级深链。
 *   /            → 文献库
 *   /?p=demo     → 阅读页
 */
function currentPaperId(): string | null {
  return new URLSearchParams(window.location.search).get('p')
}

export default function App() {
  const [paperId, setPaperId] = useState<string | null>(currentPaperId)

  useEffect(() => {
    const onPop = () => setPaperId(currentPaperId())
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
    if (id) window.scrollTo(0, 0)
  }

  if (paperId) {
    return <ReaderView paperId={paperId} onBack={() => navigate(null)} />
  }
  return <LibraryPage onOpenPaper={(id) => navigate(id)} />
}
