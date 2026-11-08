# paper-reflow research agent

DSH（DeepSeek Harness）运行时，作为 paper-reflow 的 Node sidecar 使用。

## A0 状态

已完成并验证：

- `node agent/src/bin.mjs` 启动最小 DSH 组合（`agent/minimal.yml`）。
- `GET /v1/health`、`POST /v1/sessions`、`GET /v1/sessions/:id/events`（SSE）、`POST /v1/sessions/:id/messages`、`POST /v1/sessions/:id/cancel`。
- 一次真实 turn：`reasoning.delta` → `tool.draft` → `tool.start` → `tool.end` → `text.delta` → `turn.end`。
- 进程重启后同一 session 恢复（`agents.resume`），turn 继续递增。
- Tauri `agent_sidecar.rs` 编译通过；web 的 `?research=spike` 页面构建通过。

## 为什么不用 `dsh --profile`

DSH 的 profile 引导会在 `$DSH_HOME/profiles/node_modules` 下为每个包建 junction。
Node 24（Windows）把 junction 报成普通目录，`dsh` 的 `ensureSymlink` 因而拒绝启动：

```
dsh: ...\profiles\node_modules\@deepseek-ai\dsh exists and is not a symlink
```

A0 改为直接 `boot()`：`agent/src/bin.mjs` 加载 `minimal.yml`，不经过 profile 的
junction 修复。完整组合（`dsh-base` + 研究 bundle）暂时留到 A1/A2，等这条路径稳定后再接。

## 开发

```powershell
cd agent
pnpm install --store-dir .pnpm-store --config.node-linker=hoisted
$env:PAPER_REFLOW_LLM_KEY = "sk-..."     # 不填也能启动，turn 会报 MISSING_CREDENTIAL
.\dev.ps1
```

默认端口 `8011`，token `dev-token`。

## 冒烟测试（不消耗真实额度）

```powershell
# 终端 1：假 LLM
node agent/test/mock-llm.mjs

# 终端 2：agent 指向假 LLM
$env:DSH_HOME = "...\agent"
$env:PAPER_REFLOW_LLM_KEY = "test-key"
$env:PAPER_REFLOW_LLM_BASE = "http://127.0.0.1:8012/v1"
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
  bundle/               # @paper-reflow/dsh-research（host + tools）
  profiles/research/    # 完整组合的 profile 模板（暂未使用）
  test/mock-llm.mjs     # 假 DeepSeek SSE
  test/spike-client.mjs # 冒烟客户端
```

## 已知限制

- 还没有 compaction / skills / 会话标题 / 权限裁剪的完整组合；这些在 A1/A2 接入。
- `agent.json` 握手文件由 Tauri 写入数据目录，Python 的 `/api/research/agent` 读取；A2 会换成正式代理。
- 安装包体积与启动时间尚未在干净 Windows 上实测。
