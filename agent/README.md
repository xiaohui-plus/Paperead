# Paperead research agent

DSH（DeepSeek Harness）运行时，作为 Paperead 的 Node sidecar 使用。

## A0 状态

已完成并验证：

- `node agent/src/bin.mjs` 启动最小组合（`agent/minimal.yml`）和完整组合
  （`PAPEREAD_AGENT_COMPOSE=full`，`dsh-base` + `@paperead/dsh-research`）。
- `GET /v1/health`、`POST /v1/sessions`、`GET /v1/sessions/:id/events`（SSE）、`POST /v1/sessions/:id/messages`、`POST /v1/sessions/:id/cancel`。
- 一次真实 turn：`reasoning.delta` → `tool.draft` → `tool.start` → `tool.end` → `text.delta` → `turn.end`；完整组合还产生 `title.update`。
- 进程重启后同一 session 恢复（`agents.resume`），turn 继续递增。
- Tauri `agent_sidecar.rs` 编译通过；web 的 `?research=spike` 页面构建通过。
- 完整组合的关键是禁用 `hmr`（官方 `dsh-web-app` 也这么做）；不禁用会拖住启动。

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
  profiles/research/    # 完整组合的 profile 模板（暂未使用）
  test/mock-llm.mjs     # 假 DeepSeek SSE
  test/spike-client.mjs # 冒烟客户端
```

## 已知限制

- 完整组合已能启动，但 skills/todo/compaction/title 还没有逐项回归。
- `agent.json` 握手文件由 Tauri 写入数据目录，Python 的 `/api/research/agent` 读取；A2 会换成正式代理。
- 安装包体积与启动时间尚未在干净 Windows 上实测。
- Tauri 端到端仍卡在现有 Python sidecar 的 `/api/health` 超时（不是 agent 本身）。
