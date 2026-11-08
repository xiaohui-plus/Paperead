# paper-reflow 桌面版设计

- 状态：已获用户批准（2026-11-08）
- 目标平台：Windows 10/11 x64（首发）
- 许可证：AGPL-3.0（整个仓库，含 Tauri 壳）
- 路线：Tauri 2 壳 + Python FastAPI sidecar

## 1. 背景与目标

paper-reflow 目前是「FastAPI 本地服务 + React 构建产物 + `data/` 目录」的 local-first
Web 应用。使用者需要自己装 Python、建虚拟环境、跑 `start.bat`，再打开浏览器。

桌面版的目标是让其他研究者**下载一个安装包、双击安装、双击启动**，不需要 Python、
Node 或命令行；同时保留现有的全部能力：PDF 版面重排、按页流式翻译、导读、问答、
脉络、引用溯源、导出 Markdown。

## 2. 范围

### MVP（v0.1.0-beta）

- Windows 10/11 x64 的 per-user NSIS 安装包。
- 不依赖系统 Python/Node；sidecar 自带 CPython 运行时。
- 开始菜单/桌面快捷方式，单实例启动，1–2 秒出窗口。
- 首次启动引导配置模型（预设 + 自定义 + 测试连接），可先看示例论文。
- 拖入 PDF 或“添加文章”导入；解析、翻译、导读、问答、脉络、导出可用。
- 应用内检查更新；升级保留用户数据。
- 卸载后程序目录清空，数据目录默认保留并在文档中说明。

### 非目标（本版不做）

- macOS / Linux。
- 账号体系、云同步、多人协作。
- 内置模型或代付 API 费用；仍由用户自带 Key。
- 付费/授权系统。
- 抢占 `.pdf` 默认打开方式（只注册“打开方式”，不抢默认）。
- 扫描件 OCR。

### 成功标准

- 在干净 Windows 11（无 Python/Node）上安装、启动、导入、翻译、重启后数据仍在。
- 中文用户名/中文路径/带空格路径正常。
- 退出后无残留 `paper-reflow-server.exe` 进程。
- 端口冲突时自动换端口。
- 0.1.0 → 0.1.1 应用内升级成功。
- 断网启动不崩溃，AI 功能给出明确提示。

## 3. 架构

```
Tauri 2 壳 (Rust)
  ├─ 选空闲端口 + 生成随机 token
  ├─ 启动 sidecar: paper-reflow-server.exe
  │    env: PAPER_REFLOW_PORT / PAPER_REFLOW_DATA / PAPER_REFLOW_TOKEN
  ├─ 轮询 /api/health 直到 200
  └─ 打开窗口: http://127.0.0.1:<port>
        └─ FastAPI 托管 web/dist + 全部 /api
```

### 为什么窗口加载本地 HTTP，而不是 `tauri://`

- 现有前端的相对 `/api`、SSE、`<img src="/api/...">`、`?p=` / `#b0013` 深链零改动。
- 同源，生产环境没有 CORS。
- 以后换 Electron/pywebview，后端与前端都不用动。

### 进程与生命周期

- Tauri 启动时在 Rust 侧 `TcpListener::bind("127.0.0.1:0")` 取空闲端口，关闭监听后
  把端口通过环境变量传给 sidecar；sidecar 绑定失败则退出，壳换端口重试。
- 壳生成 128-bit 随机 token，sidecar 通过环境变量接收。
- 壳轮询 `/api/health`（带 `X-Paper-Reflow-Token`），200 后显示主窗口；失败重试
  最多 10 秒，连续 3 次启动失败弹出原生错误框（重试 / 打开日志 / 退出）。
- sidecar 放入 Windows Job Object（`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`），壳退出或
  崩溃时整棵进程树被终止，避免孤儿进程。
- `tauri-plugin-single-instance`：第二次启动聚焦已有窗口；带 PDF 路径启动时把路径
  交给已运行实例。
- 日志：`%LOCALAPPDATA%\paper-reflow-data\logs\app.log` 与 `server.log`，滚动 5MB × 3；
  不记录 API Key。

### 前端与原生能力的边界

- v1 前端**不调用 `@tauri-apps/api`**，避免远程 origin 的 IPC 权限问题。
- 原生菜单负责：打开 PDF、打开数据目录、设置、关于、退出。
- 网页内仍用 HTML5 `<input type="file">` 与拖拽（Tauri 窗口设
  `dragDropEnabled: false`，让 HTML5 收到文件）。
- 原生菜单“打开 PDF”由 Rust 调 `tauri-plugin-dialog`，再 `POST /api/papers/import`
  给 sidecar，成功后 `webview.navigate` 到 `/?p=<id>`。

## 4. 安全

- 只绑定 `127.0.0.1`，不监听 `0.0.0.0`。
- 首页响应下发 `pr_token` cookie：`HttpOnly; SameSite=Strict; Path=/`。
- `/api/*` 中间件校验 cookie 或 `X-Paper-Reflow-Token`；`/api/health` 接受 header。
- 校验 `Origin`（如存在）必须是 `http://127.0.0.1:<port>`；开发模式额外允许
  `http://localhost:5173`。
- 生产环境移除宽松 CORS。
- 外部链接（DOI、网页来源）由 Tauri `on_navigation` 拦截：同源放行，外部 `http(s)`
  用系统默认浏览器打开，不在应用内导航。
- API Key：v1 继续存 WebView 的 localStorage（仅本机、无第三方脚本）；P2 迁移到
  Windows 凭据管理器（Python `keyring`）。
- 无遥测；日志只在本机。

## 5. 数据

- 数据根目录：`%LOCALAPPDATA%\paper-reflow-data`，通过 `PAPER_REFLOW_DATA` 传给 sidecar。
- 结构沿用现有 `data/`：`library.json`、`papers/{id}.pdf|.blocks.json|.thumb.png|...`。
- 安装目录：`%LOCALAPPDATA%\paper-reflow`（Tauri NSIS currentUser 默认），per-user，不弹 UAC。
  数据目录与安装目录分开，避免卸载/升级删掉文献库。
- `library.json` 改为原子写：临时文件 + `os.replace`，并在 `backups/` 保留上一版。
- 增加 `schema_version`，升级时做迁移；MVP 只需写入版本号。
- 卸载默认保留数据；应用内提供“打开数据目录”。

## 6. 打包与发布

### Sidecar

- PyInstaller **onedir + noconsole**，禁止 onefile（启动慢、杀软误报、每次解压）。
- 包含 `web/dist`、PyMuPDF 的 MuPDF 库、`certifi`、Python 运行时。
- 加 Windows 版本资源（`version_info.txt`）和图标。
- 预计 80–120MB。

### Tauri

- `bundle.targets = ["nsis"]`；`installMode = "currentUser"`。
- `webviewInstallMode = "embedBootstrapper"`（安装时按需下载 WebView2）。
- `externalBin` 放 sidecar；文件按 target triple 命名：
  `paper-reflow-server-x86_64-pc-windows-msvc.exe`。
- 安装包预计 90–130MB。

### CI / 发布

GitHub Actions `windows-latest`：

1. `npm ci` + `npm run build`（web）。
2. `uv` 建干净 CPython 3.12 venv（不用 MinGW/Conda），装 `.[server]` + PyInstaller。
3. PyInstaller 产出 sidecar，复制到 `src-tauri/binaries/`。
4. `npm run tauri build`，可选签名。
5. 上传 `setup.exe`、`.sig`、`latest.json`、`sha256.txt`，创建 Release。

- 版本号单一来源脚本同步：`pyproject.toml`、`web/package.json`、根 `package.json`、
  `src-tauri/tauri.conf.json`、`src-tauri/Cargo.toml`。
- 更新：`tauri-plugin-updater`，minisign 私钥放 GitHub Secrets，公钥写进配置；
  私钥必须离线备份。
- 国内下载：GitHub Releases 为主，另备对象存储镜像（Cloudflare R2 或阿里云 OSS）；
  更新清单走自定义域名，不直接依赖 GitHub。

## 7. 首次启动与升级体验

首次启动三步：

1. 说明“PDF 和数据只在本机；翻译时会把论文文本发给你配置的模型服务商”。
2. 选择预设（GLM / Qwen / 豆包 / DeepSeek / 自定义）或填自定义；测试连接；保存。
3. “打开示例论文”或“选择 PDF”。

升级：启动 5 秒后静默检查；有新版弹 toast；用户确认后下载、退出、替换、重启；
数据目录不动。

## 8. 合规

- 仓库整体改为 AGPL-3.0：`LICENSE`、`pyproject.toml`、`README`、`web/package.json`、
  `src-tauri/Cargo.toml`。
- “关于”对话框放：版本、AGPL 声明、源码地址、第三方许可入口。
- `THIRD_PARTY_NOTICES.md` 汇总：PyMuPDF（AGPL）、KaTeX（MIT）、React（MIT）、
  FastAPI（MIT）、Tauri（MIT/Apache）、Python（PSF）等。
- `PRIVACY.md`：不收集遥测；PDF 本地处理；论文文本会发给用户配置的模型服务商；
  联网检索可选。
- 每个 Release 的源码 tag 与构建说明必须可用。

## 9. 签名

- Windows 未签名会触发 SmartScreen；正式版必须签名。
- 开源项目可申请 SignPath 等免费 OSS 签名；或购买 OV 证书（约 $200–400/年）。
- beta 可先不签，Release 页给 SHA256 和手动放行说明；正式版前补签名。
- macOS 本版不做，无需 Apple 开发者账号。

## 10. 里程碑

| 阶段 | 内容 | 预估 |
|---|---|---|
| M0 | `git init` + AGPL + 干净 venv + PyInstaller 产出可运行 sidecar | 1 天 |
| M1 | Tauri 壳：动态端口/token、启动/健康检查/退出、单实例、日志 | 2 天 |
| M2 | NSIS 安装包、数据目录、图标、首次启动、原生打开 PDF、打印验证 | 2 天 |
| M3 | GitHub Actions、版本同步、自动更新、0.1.0-beta 发布 | 2 天 |
| M4 | 原子写、错误恢复、干净 VM/中文路径/离线测试、文档 | 2–3 天 |

## 11. 验收清单

- [ ] 干净 Windows 11（无 Python/Node）安装、启动、导入、翻译、重启后数据仍在。
- [ ] 中文用户名、中文路径、带空格路径正常。
- [ ] 退出后任务管理器没有残留 `paper-reflow-server.exe`。
- [ ] 端口被占用时自动换端口。
- [ ] 断网启动不崩，AI 功能给出明确提示。
- [ ] 0.1.0 → 0.1.1 应用内升级成功。
- [ ] 卸载后程序目录清空，数据目录保留（文档写明路径）。
- [ ] Defender 不报毒（签名后复测）。
- [ ] `pytest` 全绿；sidecar smoke test 通过。

## 12. 风险

| 风险 | 影响 | 对策 |
|---|---|---|
| PyMuPDF AGPL | 闭源分发违规 | 已选 AGPL 开源；未来闭源需买授权或换引擎 |
| 未签名 exe | SmartScreen/杀软拦截 | onedir、版本资源、尽早签名 |
| WebView2 缺失 | Win10 旧机器装不上 | 安装器带 bootstrapper |
| 打印/拖拽差异 | WebView 行为不一致 | M2 先验证；打印走后端 PDF，拖拽用 HTML5 |
| 国内下载慢 | 安装/更新失败 | 对象存储镜像 + 自定义更新域名 |
| 端口/孤儿进程 | 启动失败/残留 | 动态端口 + Job Object |

## 13. 后续

- macOS（Developer ID + 公证）、Linux（AppImage/deb）。
- API Key 迁移到 Windows 凭据管理器。
- Tavily Key 设置项（现在只认环境变量）。
- 代码签名正式化。
- 崩溃诊断包导出。

## 14. 环境基线（开发机，2026-11-08）

- Windows 11 24H2（10.0.26100），x64。
- Rust 1.96.0、MSVC Build Tools 18、WebView2 157。
- Node 24.11、npm 11.6、Python 3.12、uv 0.11。
- 缺：Tauri CLI、PyMuPDF、`web/node_modules`；项目尚未 `git init`。
- 构建使用独立干净 CPython 3.12 venv，不使用 MinGW/Conda 的 Python。
