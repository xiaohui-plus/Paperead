# DSH 接入 seam 笔记（P0.5）

> 日期：2026-11-08  
> 运行时契约：`agent/node_modules/@deepseek-ai/*`（`0.1.0-rc.6`，以其中的 `.d.ts` 为准）  
> 语义参考：`F:\Agent\dsh-ref-rc7`（GitHub `dsh-v0.1.0-rc.7`，commit `99f6f02`）  
> 上游文档：[DSH 接入完整方案](../../docs/superpowers/specs/2026-11-08-dsh-integration-solution.md)

这份笔记只记录**我们依赖的接口**、**不能碰的内部实现**和**已经踩到的坑**。不追求覆盖 DSH 全貌。

---

## 1. boot / app-boot

### 我们依赖的契约

```ts
boot(
  binName: string,
  absoluteConfigPath: string,
  patches?: PatchOptions[],
  prepare?: (ctx: Context) => Promise<void> | void,
  bareModuleBaseUrl?: string,
): Promise<Context>

loadOptionalPatches(binName: string, file: string): PatchOptions[] | undefined
loadOverlayPatches(binName: string, file: string): PatchOptions[]
```

- `boot()` 返回时整棵 loader 树已经 settle；失败会先 dispose 半个 context 再抛错。
- `patches` 是一个扁平列表，按顺序应用；**同一 row 后写覆盖前写**。
- patch 条目只有两种形态：按 `id` 覆盖（`config` / `disabled` 等），或 `insert` 一个 row 列表。
- **patch 替换整个 `config`，不与原 config 深合并。** 我们要改某 row 的一个字段，必须把该 row 的完整 config 再写一遍。
- row 顺序没有加载语义，激活是服务可用性驱动的；不要靠排列顺序控制启动。
- `loadOptionalPatches`：文件不存在返回 `undefined`；存在但解析失败/不是数组会抛错（必须 fail loud）。
- 退出时 `ctx.fiber.dispose()`；`bin.mjs` 现在就是这么做的。

### 不能碰

- 不复制 `apps/cli` 的 profile 逻辑，不改 `node_modules` 里的 `ensureSymlink`。
- 不依赖 loader 的内部 row 索引；只按 `id` 覆盖公开 row。

### 坑

- Node 24 / Windows 下 `dsh --profile` 的 junction 修复会失败，所以直接 `boot()`。
- `port: 0` 时 `webServer.port` 才是 OS 分配的真实端口；日志/健康检查必须读这个 getter，不能读配置值。
- 研究 patch 里 `hmr` 必须 `disabled: true`，否则完整组合启动被 watcher 拖住。

---

## 2. core/agent：会话生命周期

### 我们依赖的契约

```ts
ctx.agents.create({
  sessionId: SessionId,
  meta?: { cwd?, parentSession?, seedLength?, origin?, delegationDepth?, agentPreset? },
  seed?: readonly SessionEvent[],
  agentOptions?: { provider?, model?, maxTokens? },
  signal?: AbortSignal,
  setup?: AgentSetup,
}): Promise<AgentHandle>

ctx.agents.resume({
  resumeSessionId: SessionId,
  agentOptions?: AgentOptions,
  signal?: AbortSignal,
  setup?: AgentSetup,
}): Promise<AgentHandle>

ctx.agents.get(id): Agent | undefined
ctx.agents.roots(): Agent[]

age: Agent {
  id: SessionId
  status: 'idle' | 'running'
  session: Session
  followup(message: UserMessage): void
  cancel(cause: AgentCancelCause, options?: { keepInbox?: boolean }): void
  whenIdle(): Promise<void>
  runMaintenance<T>(task: (signal: AbortSignal) => Promise<T>): Promise<T>
}
```

- `create/resume` 的 `setup` 在 session/agent 发布**之前**执行；setup 抛错会整体回滚，不会泄漏半个会话。
- `setup` 只用于组合（注册 scoped 工具、prompt section、监听器），**不能在里面驱动 agent**；驱动要等 `create/resume` resolve 之后。
- `followup()` 是即发即忘：入队并唤醒 driver，不返回 Promise。要等一轮结束用 `agent.whenIdle()` 或监听 `turn/end`。
- `cancel()` 的 cause 是判别联合：`{kind:'user'} | {kind:'parent'} | {kind:'hook',reason} | {kind:'disposed'}`。
- `resume` 要求 session 已经持久化；首次使用应先 `create`。

### 不能碰

- 不直接构造 `Agent` / `Session`，不调用 loop 内部 driver。
- 不把 `setup` 当 turn 驱动入口。

### 坑

- `resume` 对不存在的 session 会 reject，所以 `ensureAgent` 先 resume、失败再 create 的写法是对的，但要把失败原因记录到日志，避免把“磁盘损坏”误判成“首次创建”。
- `ctx.agents.get()` 只能拿到**本进程**已注册的 agent；进程重启后必须走 `resume`。
- `whenIdle()` 只代表当前活动收敛，不标识某条消息的完成；产品语义用 `turn.end`。

---

## 3. session / 持久化 / 事件

### 我们依赖的契约

订阅：

```ts
ctx.on('session/event', (session, event) => { ... })
ctx.on('session/flush', async (session) => { ... })
```

`SessionEvent` 信封：

```ts
{
  type: SessionEventType
  seq: number        // 会话内单调递增，连续
  time: number       // epoch ms
  data: SessionEventMap[type]
  optional?: boolean // 不认识的类型带此标记才允许跳过
}
```

关键事件与 `data`（rc.6）：

| 事件 | data |
|---|---|
| `turn/start` | `{ turn: number }` |
| `turn/end` | `{ turn: number; reason: TurnEndReason }` |
| `step/start` / `step/end` | `{ turn; step }` |
| `user/message` | `UserMessage`（`content` + `source`） |
| `assistant/chunk` | `{ turn; step; chunk: StreamChunk }` |
| `assistant/message` | `{ turn; step; message; usage?: TokenUsage }` |
| `tool/call` | `{ turn; step; callId; name; arguments: string }`（arguments 是原始 JSON 字符串） |
| `tool/result` | `{ turn; step; message: ToolResultMessage; error?: {name,code}; meta?: JsonValue }` |
| `todo/write` | `{ todos: TodoItem[] }`，`TodoItem = { content; status: 'pending' \| 'in_progress' \| 'completed' }` |
| `session/title` | `{ title; messageSeqs: number[]; source }` |
| `compaction/start` | `{ compactionId; sourceCommandId?; turn: number \| null }` |
| `compaction/summary` | `{ compactionId; summary; shadowedRange; shadowedSeqs; shadowedTokenCount; provider; model; ... }` |
| `compaction/end` | `{ compactionId; turn; error? }` |
| `compaction/prune` | `{ shadowedRange; shadowedSeqs; shadowedTokenCount }` |

`TurnEndReason`（判别联合，`kind` 为判别字段）：

```ts
{ kind: 'completed' }
{ kind: 'aborted'; reason: AgentCancelCause | { kind: 'legacy' } }
{ kind: 'blocked' }
{ kind: 'error'; error: LlmFailure }
{ kind: 'max-tokens' }
{ kind: 'interrupted' }   // 持久化层在 reload 时补记的崩溃孤儿 turn
```

`assistant/chunk` 的 `StreamChunk`：

```ts
{ type:'block-start'; index; blockType }
{ type:'text-delta'; index; text }
{ type:'reasoning-delta'; index; text }
{ type:'tool-call-delta'; index; id; name?; argumentsDelta }
{ type:'block-end'; index; block }
{ type:'usage'; usage }
{ type:'finish'; reason; replayState? }
```

### 行为

- `session/event` 是 **post-commit、fire-and-forget** 的 append feed：回调在日志 push 之后运行，监听器失败只记录、不会让 append 失败。
- 因此它**不是**持久化确认；要等落盘用 `session/flush`（awaited parallel checkpoint）。
- 事件是会话日志的原文，回放和实时是同一份数据；`tool/result.meta` 必须是可 JSON 序列化的值，否则 `Session.append` 直接拒绝。
- 持久化默认在 `$DSH_HOME/sessions`（`dshHomePath('sessions')`）；zstd jsonl，可 resume。
- 崩溃后的 open turn 由持久化层补 `interrupted`，旧事件保留。

### 不能碰

- 不自己写 session jsonl，不修改已知事件类型的语义。
- 不把 `session/event` 当 ack；需要“已保存”时 await `session/flush`。

### 坑（已影响现有代码）

- `turn.end.reason` 现在是**对象**（`{kind:'completed'}`），不是字符串；SSE 投影若原样透传，前端必须按 `kind` 判断。
- `tool/result` 事件本身**没有 callId**，要配对的 `callId` 在 `message.content` 的 tool-result block 上；现有投影只取 `content[0]`，遇到非 tool block 会丢 callId，应扫描全部 block 找 `toolCallId`。
- `tool-call-delta` 的第一片可能没有 `name`；`tool.draft` 的 `name` 允许为空，UI 要容忍。
- `session/title` 同时存在 fallback 与 LLM provider：即使标题模型失败也会有 `fallback` 标题事件，所以回归脚本不能只靠“有 title 事件”证明 LLM 标题链路通。

---

## 4. tool/tools：工具定义

### 我们依赖的契约

```ts
defineTool({
  name: string
  description: string
  parameters: ParameterSchemaSpec     // 每个属性的 schema，编译成隐式开放对象根
  output: {
    schema: ValueSchemaSpec           // 成功返回值的强制 schema
    render(args, value): ContentBlock[]
    presentationMeta?(args, value): JsonValue
  }
  timeoutMs?: number
  isConcurrencySafe?(args): boolean
  execute(args, exec: ToolRunContext): Promise<unknown>
  finalizeContent?(exec, result): ContentBlock[] | undefined
  presentCall?(args): ToolCallView | undefined
  presentResult?(args, result: ToolResult): ToolResultView | undefined
})
```

- `execute` 的第二个参数含 `exec.signal`；异步工具必须转发/观察取消信号，并在自己拥有的工作收敛后再 settle。
- `presentCall` / `presentResult` **必须是纯函数**：实时流和日志回放都会调用，只能依赖 `args` 和 `result`。
- `presentationMeta` 只在顶层调用计算，随 `tool/result.meta` 持久化；回放时原样可用。
- `ToolResult = { content: ContentBlock[]; isError: boolean; meta?: JsonValue }`。
- `ToolCallView`：`generic`（`title` / `kind?` / `rawInput?` / `content?` / `locations?`）、`terminal`、`diff`。
- `ToolResultView`：`generic`（`title?` / ...）、`terminal`、`diff`、`search`、`read`、`web`。
- `ToolCallKind`：`read | edit | delete | move | search | execute | fetch | other`。

### 不能碰

- 不绕过 `defineTool` 直接塞 `ToolDefinition`，不依赖 registry 内部字段。
- 不在 `presentCall/presentResult` 里读文件、发请求或读时钟。

### 坑

- `presentationMeta` 必须可 JSON 序列化；`Date`、`Map`、`undefined` 字段会出问题。
- 模型给的工具参数是**原始 JSON 字符串**拼出来的；`tool-call-delta` 要自己做容错解析。
- `timeoutMs` 只在工具真正转发 `exec.signal` 时才有意义；声明了却不响应取消会拖住 turn。

---

## 5. host/webserver

### 我们依赖的契约

```ts
ctx.webServer.register({
  kind: 'exact' | 'prefix',
  path: string,                     // 绝对 pathname，无尾斜杠
  handler: (req: IncomingMessage, res: ServerResponse) => void | Promise<void>,
}): () => void

ctx.webServer.port  // config.port === 0 时为 OS 分配端口
ctx.webServer.registerUpgrade({ path, handler })
ctx.webServer.registerFallback(handler)
ctx.webServer.tapIndex(transform)
```

- handler 拥有完整的响应生命周期，可以一直持有连接做 SSE。
- 重复注册同一 `(kind, path)` 会抛错；注册顺序不影响匹配。
- 监听失败会让插件 fiber 失败，`boot()` 报错。
- 注册返回 disposer；插件 dispose 时路由自动移除。

### 不能碰

- 不在 handler 里假设 DSH 会替你收尾；SSE 的计时器、连接集合、`close` 清理都要自己管。
- 不注册与官方 web bundle 相同的 path。

### 坑

- `port: 0` 时日志里打印配置端口会得到 0；必须用 `ctx.webServer.port`。
- SSE 需要显式 `x-accel-buffering: no`、`cache-control: no-transform`，并且每 15s 心跳，否则代理/浏览器会攒包。

---

## 6. title 与 compaction：额外 LLM 调用

### session-title-first-prompt-llm

- 输入是**第一条人类文本消息**，最多 `maxInputBytes`（dsh-base 默认 4096）。
- 用 `maxOutputTokens`（默认 64）做一次辅助请求；失败/超时会退回 `session/title` 的 fallback 标题。
- 事件来源在 `source.kind`：`fallback` | `provider` | `user`。
- 回归脚本若要断言“LLM 标题链路”，必须检查 `source.kind === 'provider'`，并让假 LLM 对这个辅助请求回标题文本。

### compaction-basic

- 默认 `auto: true`，在 step 边界按模型上下文窗口的 `thresholdRatio`（默认 0.8）触发；保留 `retainRatio`（默认 0.16）或 `retainTokens`。
- 摘要默认继承对话 target，可用 `summarizationProvider/Model` 覆盖；`maxTokens` 默认 8192。
- 摘要调用走同一条 LLM seam，因此假 LLM 要能识别“压缩请求”（system/prompt 特征）并返回摘要。
- 事件序列：`compaction/start` →（LLM 摘要）→ `compaction/summary` → `user/message`（替换面）→ `compaction/end`。
- `compaction/summary` 与紧随其后的 `user/message` 的相邻关系是契约，回放/计量都依赖它。

### 我们不能碰的

- 不改 compaction 的锁语义；不并发触发手动 compact。
- 不把 `compaction/summary` 直接投影成 UI 消息；用户看到的是后面那条 replacement `user/message`。

---

## 7. `dsh-base` row 清点（rc.6）

来源：`agent/node_modules/@deepseek-ai/dsh-base/cordis.patch.yml`。

### 会发起 LLM 请求

| row id | 说明 |
|---|---|
| `llm-deepseek` | 对话主适配器 |
| `session-title-llm`（`session-title-first-prompt-llm`） | 首条消息后一次标题请求 |
| `compaction-basic` | 触发压缩时一次摘要请求 |
| `llm-retry` | 失败重试包装，会放大请求次数 |

### 会起 watcher / 子进程 / 网络

| row id | 说明 | 我们的处理 |
|---|---|---|
| `hmr` | 全树 watcher | `disabled: true` |
| `tool-bash` / `tool-pwsh` | 本地 shell | 禁用 |
| `tool-jobs` | 后台任务 | 禁用 |
| `subprocess` / `jobs` | 子进程与任务服务 | 保留服务，但不向模型暴露工具 |
| `skill-filesystem` | 技能目录 watcher（`watch` 默认开） | 保留；打包后要确认路径 |
| `session-telemetry-otel` | OTLP 上报 | 禁用 |
| `web-search-deepseek` / `tool-web` | 外部检索 | 保留，`fetch: false` |
| `workflow-worker-thread` / `subagent-*` | worker/子代理 | 工具禁用，服务保留 |
| `sandbox-policy` / `approval` / `permission` | 权限询问链路 | 保留，研究工具不走 shell |

### 我们保留、且产品直接依赖

`tool-todo`、`tool-skill`、`tool-web`、`session-title`、`session-title-first-prompt-llm`、`session-persistence-jsonl`、`compaction-basic`、`tool-result-pruner`、`session-checkpoint-policy`、`token-meter`、`timeout-policy`、`spill-policy`、`attachment-local`。

> 注意：`tool-goal` 已禁用，但 `goal` / `goal-round-driver` / `command-goal` 仍在。工具不可见，模型无法启动 goal；保留服务不影响研究语义。`plan-mode` 禁用后，plan-mode 的 prompt section 也不会注入。

---

## 8. `session/event → 产品 SSE` 终稿映射

| DSH 事件 | 产品 SSE | 备注 |
|---|---|---|
| `turn/start` | `turn.start` | `turn` |
| `turn/end` | `turn.end` | `reason` 必须保留对象；前端读 `reason.kind` |
| `assistant/chunk` `reasoning-delta` | `reasoning.delta` | 50ms 合并 |
| `assistant/chunk` `text-delta` | `text.delta` | 30ms 合并 |
| `assistant/chunk` `tool-call-delta` | `tool.draft` | `name` 可能缺省；参数分片累积 |
| `tool/call` | `tool.start` | `arguments` 是字符串，需要容错解析 |
| `tool/result` | `tool.end` | 扫描全部 content block 取 `toolCallId`；带 `meta` |
| `todo/write` | `plan.update` | 整表快照 |
| `session/title` | `title.update` | 带 `source.kind` |
| `compaction/start` | `notice`（可选） | 不建议做成用户消息 |
| `compaction/end` | `notice`（可选） | 失败时带 `error` |
| `session/end-seed` | 忽略 | 回放边界 |
| 其它 | 忽略 | 不认识的 `optional !== true` 时要告警 |

SSE 信封保持：`{ seq, at, type, ... }`，每会话单调；`after` 续传；15s `ping`。

---

## 9. 升级 DSH 时必须跑的契约测试

1. `boot()` 完整组合可启动（`hmr` 禁用），`/v1/health` 200。
2. 一轮最小 turn：`turn.start → reasoning.delta → tool.draft → tool.start → tool.end → text.delta → turn.end`。
3. `tool.end` 能配到 `callId`，`meta` 可在回放中复原。
4. `todo_write` 产生 `plan.update`，`todos` 三态完整。
5. 标题事件出现，且 `source.kind` 可为 `provider`（假 LLM 专用响应）。
6. `compaction/*` 事件顺序正确，replacement `user/message` 紧邻 `compaction/summary`。
7. 进程重启后 `resume` 成功，`turn` 递增，历史事件可回放。
8. 取消：`cancel({kind:'user'})` 后收到 `turn/end.reason.kind === 'aborted'`，保留半截文本。
9. 禁用 row 不因升级被重新启用（对 `hmr` / shell / fs / subagent 做断言）。
10. `webServer.port` 与配置端口在 `port: 0` 时一致（读 getter）。

> 升级流程：锁 `0.1.0-rc.x` → 在独立分支跑以上 10 条 → 通过后单独 PR；不随功能 PR 混升。
