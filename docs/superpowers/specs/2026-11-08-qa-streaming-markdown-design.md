# 问答流式输出 + Markdown 文档渲染 · 设计

- 版本：v1（已确认）
- 日期：2026-11-08
- 范围：阅读页左侧「问答」tab 的回答呈现与传输方式
- 关联：`web/src/components/ChatPanel.tsx`、`web/src/lib/library.ts`、`server/main.py` `/ask`、`paperead/tutor/qa.py`、`paperead/llm.py`

> 一句话：把左侧问答从「一次性返回 + 纯文本气泡」改成「逐字流式 + 成品 Markdown 文档」。
> 视觉参考：靠岸学术右侧 AI 回答——无气泡、标题/列表/表格/公式按文档排版。

---

## 1. 现状

- `ChatPanel` 用 `messages` 数组存纯文本，AI 回答渲染在 `.msg.ai` 气泡里，`white-space: pre-wrap`。
- `POST /api/papers/{id}/ask` 先检索、再一次性 `chat()`，回答完整后才返回。
- SSE 只存在于按页翻译链路（`/translate/stream`）。

## 2. 目标与非目标

### 2.1 目标

1. 回答逐字流式出现，首字延迟只受首个 token 影响。
2. 回答按 Markdown 渲染为文档：标题、列表、引用、代码块、GFM 表格、KaTeX 行内/块级公式。
3. 用户提问保留蓝色气泡（右对齐），AI 回答去掉气泡/边框/背景。
4. 流式期间可停止；断流时保留已收到的内容。

### 2.2 非目标

- 不做对话持久化、多轮记忆、跨论文记忆（见 `2026-11-08-qa-memory-design.md`）。
- 不修改检索策略、`needs_external` 判定、出处规则。
- 不做「重新生成」、消息编辑、导出对话。
- 不改变模型配置的存储方式（仍只在 localStorage）。

## 3. 交互

- 发送后立即在答案区显示「检索论文并思考中…」（检索 + 查询改写阶段）。
- 来源先到：收到 `sources` 事件即渲染出处 chips；正文随后逐字出现。
- 流式光标：生成中以「▍」结尾；出字前显示「正在生成回答…」。
- 完成后在答案底部显示 `用时 x.xs · 已联网检索`（仅联网时显示后半句）。
- 自动贴底；用户上滚超过 48px 时暂停跟随，回到底部自动恢复。
- 生成中发送键变「停止」，点击中止前端请求并取消后端上游生成。
- 出错：保留已收到内容，下方显示「回答中断：…」；停止显示「已停止生成」。
- 未配置模型：保留原来的本地演示答案与「先配置模型」提示，演示文本按纯文本渲染。

## 4. 传输协议

`POST /api/papers/{paper_id}/ask/stream`，请求体与 `/ask` 相同：

```json
{ "config": { "api_key": "...", "base_url": "...", "model": "..." }, "question": "...", "web": true }
```

响应 `text/event-stream`，`data:` 行 JSON：

| type | 载荷 | 说明 |
|---|---|---|
| `status` | `{"stage":"retrieving"}` | 开始检索（立即发送） |
| `sources` | `{"sources":[...],"usedWeb":bool}` | 检索完成、开始生成 |
| `delta` | `{"text":"..."}` | 增量文本，可重复 |
| `done` | `{"latency":4.2}` | 正常结束 |
| `error` | `{"message":"..."}` | 出错；已发送的增量保留 |

- 校验失败（论文不存在 / 解析中 / 问题为空 / 未配置模型）在返回流之前以普通 HTTP 4xx 返回。
- 客户端断开时 Starlette 取消生成器，上游 OpenAI 流随 `async with` 关闭。
- 原 `POST /ask` 保留，行为不变。

## 5. 实现

### 5.1 后端

- `paperead/llm.py`
  - `OpenAICompatBackend.chat_stream()`：`stream=True`，逐 delta 产出 `content`（忽略仅带 usage 的 chunk），结束时关闭上游。
  - 模块级 `chat_stream()`：后端没有 `chat_stream` 时退回 `chat()` 一次性输出，保证测试注入的假后端仍可用。
- `paperead/tutor/qa.py`
  - 抽出 `_answer_messages()`，`answer()` 与新增的 `answer_stream()` 共用同一 prompt。
- `server/main.py`
  - 把检索、来源构造抽成 `_prepare_ask()`；`_require_ask_ready()`、`_apply_llm_config()` 消除重复。
  - `/ask` 改用上述 helper；新增 `/ask/stream`，按上表发事件。

### 5.2 前端

- `web/src/components/Markdown.tsx`
  - `react-markdown` + `remark-gfm` + `remark-math` + `rehype-katex` + `remark-breaks`。
  - 默认不渲染原始 HTML（防注入）；链接新窗口打开；表格外包横向滚动容器。
- `web/src/components/ChatPanel.tsx`
  - `turns` 状态（question / answer / sources / status / latency / usedWeb）。
  - `AbortController` 支持停止；增量按 50ms 合并后重渲染，避免逐 token 抖动。
- `web/src/lib/library.ts`
  - 抽出通用 `readSSE()`（翻译与问答共用）；新增 `askPaperStream(..., signal)`。
  - 旧服务 / 旧桌面 sidecar 没有 `/ask/stream`（404）时自动退回 `/ask`，整段返回后照常按 Markdown 渲染，只是不再逐字出现。
- `web/src/styles.css`
  - 新增 `.turn` / `.turn-q` / `.turn-a` / `.md` 排版；移除旧 `.msg.ai` 气泡样式。

## 6. 边界与风险

- **流式半截 Markdown**：未闭合的代码块/表格由解析器按普通内容处理，收尾自然补齐；不额外修补语法。
- **窄面板**：块级公式与表格横向滚动；代码块 `overflow-x:auto`。
- **Markdown 不完整**：模型输出非 Markdown 的连续短行由 `remark-breaks` 保留换行，与旧 pre-wrap 观感一致。
- **XSS**：回答来自模型，react-markdown 默认跳过 HTML，不放行 `dangerouslySetInnerHTML`。
- **请求取消**：前端 abort + 后端 `CancelledError` 透传，不写半截状态。
- **并发**：每次请求各自持有 backend 客户端引用；沿用现有全局 backend 注入方式，不在此次范围内改造。

## 7. 测试策略

- pytest（假后端，不依赖真实模型）：
  - SSE 事件顺序 `status → sources → delta* → done`，增量拼接等于完整回答。
  - 后端没有 `chat_stream` 时退化为单条 delta。
  - 生成中途抛错 → 已发增量保留 + `error` 事件。
  - 未配置模型 → HTTP 400。
- 前端：`npm run build`（tsc 类型检查 + vite 构建）。
- 视觉：临时预览服务提供假 SSE，用 headless Chrome 在 344px 面板宽度截图核对标题/列表/表格/公式与流式状态。
- 回归：全量 `pytest`。
