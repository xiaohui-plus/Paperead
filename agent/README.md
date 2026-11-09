# Paperead research agent

DSH（DeepSeek Harness）运行时，作为 Paperead 的 Node sidecar 使用。

## 状态（A0 + P0.5 / P1 / P2）

已完成并验证：

- `node agent/src/bin.mjs` 启动最小组合（`agent/minimal.yml`）和完整组合
  （`PAPEREAD_AGENT_COMPOSE=full`，`dsh-base` + `@paperead/dsh-research`）。
- `GET /v1/health`、`POST /v1/sessions`、`GET /v1/sessions/:id/events`（SSE）、`POST /v1/sessions/:id/messages`、`POST /v1/sessions/:id/cancel`。
- 一次真实 turn：`reasoning.delta` → `tool.draft` → `tool.start` → `tool.end` → `text.delta` → `turn.end`；完整组合还产生 `title.update`。
- 进程重启后同一 session 恢复（`agents.resume`），turn 继续递增。
- Tauri `agent_sidecar.rs` 编译通过；web 的 `?research=spike` 页面构建通过。
- 完整组合的关键是禁用 `hmr`（官方 `dsh-web-app` 也这么做）；不禁用会拖住启动。
- P0.5：6 个接入 seam 的契约固化在 `agent/docs/dsh-integration-notes.md`。
- P1：`npm run test:full` 覆盖工具清单、工具卡、todo/plan、标题（provider）、
  `presentationMeta` 和 resume；shell/fs/subagent 断言为不可见。
- P2：agent 端口/token 在 Python 启动前预分配，通过环境变量注入；
  不再写 `agent.json`，Python 代理不向前端暴露 token。
- 内置技能：`agent/skills` 通过 `PAPEREAD_SKILLS_DIR` + `customSkillDirs` 接入，
  回归断言 `write-latex` 进入模型请求；用户明确要求 LaTeX 时可按技能产出
  `.tex` 并调用 `export_document(format="latex" | "pdf")` 落盘/编译。

## 为什么不用 `dsh --profile`

DSH 的 profile 引导会在 `$DSH_HOME/profiles/node_modules` 下为每个包建 junction。
Node 24（Windows）把 junction 报成普通目录，`dsh` 的 `ensureSymlink` 因而拒绝启动：

```
dsh: ...\profiles\node_modules\@deepseek-ai\dsh exists and is not a symlink
```

A0 改为直接 `boot()`：`agent/src/bin.mjs` 加载 `minimal.yml` 或完整组合的 patch，
不经过 profile 的 junction 修复。

## 开发

```powershell
cd agent
pnpm install --store-dir .pnpm-store --config.node-linker=hoisted
$env:PAPEREAD_LLM_KEY = "sk-..."     # 不填也能启动，turn 会报 MISSING_CREDENTIAL
.\dev.ps1
```

默认端口 `8011`，token `dev-token`。完整组合：

```powershell
$env:PAPEREAD_AGENT_COMPOSE = "full"
.\dev.ps1
```

## 冒烟测试（不消耗真实额度）

一键完整组合回归（自带假 LLM、临时 `DSH_HOME`、自动清理）：

```powershell
cd agent
npm run test:full
```

手工最小组合：

```powershell
# 终端 1：假 LLM
node agent/test/mock-llm.mjs

# 终端 2：agent 指向假 LLM
$env:DSH_HOME = "...\agent"
$env:PAPEREAD_LLM_KEY = "test-key"
$env:PAPEREAD_LLM_BASE = "http://127.0.0.1:8012/v1"
node agent/src/bin.mjs

# 终端 3：一次完整 turn
node agent/test/spike-client.mjs
```

期望输出包含 `turn.start`、`reasoning.delta`、`tool.start`、`tool.end`、`text.delta`、`turn.end`。

## 目录

```
agent/
  src/bin.mjs           # 直接 boot 入口
  minimal.yml           # A0 最小组合
  bundle/               # @paperead/dsh-research（host + tools）
  skills/               # 内置技能（写综述、LaTeX 写作、找文献等）
  profiles/research/    # 完整组合的 profile 模板（暂未使用）
  test/mock-llm.mjs     # 假 DeepSeek SSE
  test/spike-client.mjs # 最小组合冒烟客户端
  test/full-regression.mjs # 完整组合回归
  docs/                 # 接入 seam 笔记
```

## 已知限制

- 完整组合已回归 tools/todo/title/meta/resume/skills 发现；compaction 的深触发仍未覆盖
  （compaction 需要真实上下文阈值，见 `docs/dsh-integration-notes.md` 的升级清单）。
- 安装包体积与启动时间尚未在干净 Windows 上实测。
