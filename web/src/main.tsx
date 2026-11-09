import React from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import 'katex/dist/katex.min.css'
import './styles.css'
import './research/research.css'
import { migrateLegacyStorage } from './lib/storage'
import { syncVisionConfig } from './lib/settings'
import { autoCheckUpdate } from './lib/update'

migrateLegacyStorage()
// 解析阶段识别公式需要识图模型配置；启动时先同步一次，避免要先打开设置页
void syncVisionConfig()
// 每天自动检查一次新版本；设置页里也能随时手动检查
void autoCheckUpdate()

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
