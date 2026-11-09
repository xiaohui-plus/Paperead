import React from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import 'katex/dist/katex.min.css'
import './styles.css'
import './research/research.css'
import { migrateLegacyStorage } from './lib/storage'
import { syncVisionConfig } from './lib/settings'

migrateLegacyStorage()
// 解析阶段识别公式需要识图模型配置；启动时先同步一次，避免要先打开设置页
void syncVisionConfig()

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
