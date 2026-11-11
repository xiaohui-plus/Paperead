# Paperead 快速开始

一个本地跑起来的论文阅读 + 理解工具：PDF 版面重排、英中逐段流式翻译、
导读、引用溯源、思路来源图、带出处的问答。

## 1. 环境要求

- Python **3.10+**
- 无需 Node（前端已构建好，随包附带）
- 一个 OpenAI 兼容的大模型 API Key（DeepSeek / 通义 / 豆包 / SiliconFlow / 本地 Ollama 都行）

## 2. 启动

**Windows**：双击 `start.bat`

**macOS / Linux**：

```bash
chmod +x start.sh
./start.sh
```

首次运行会自动创建 `.venv` 并安装依赖（约 1–2 分钟），之后每次启动只需几秒。

打开 http://127.0.0.1:8010

## 3. 配置模型

两种方式，二选一：

**A. 界面上配（推荐）**
点右上角齿轮 → 选择预设（GLM / Qwen / 豆包）或填自定义 → 保存。
配置存在浏览器本地，不会上传。

**B. 环境变量**
复制 `.env.example` 为 `.env`，填入：

```
PAPEREAD_API_KEY=sk-xxxx
PAPEREAD_BASE_URL=https://api.deepseek.com/v1
PAPEREAD_MODEL=deepseek-chat
```

## 4. 用起来

1. 首页上传 PDF（或点示例论文）
2. 自动排版 + 按页翻译：打开页面 1–2 秒出第一段中文，其余陆续补上
3. 左栏六个标签：
   - **导读**：这篇讲什么、需要什么前置、建议先读哪几篇
   - **问答**：基于论文 + 引用文献 + 网页回答，每条带出处
   - **目录** / **重点** / **图表**：跳转与速览
   - **脉络**：谁启发了本文、本文改了什么
4. 正文段落 hover 出现「引」按钮 → 这段话的出处原文

## 5. 可选配置

| 变量 | 作用 |
|---|---|
| `TAVILY_API_KEY` | 可选。配置后联网检索优先走 Tavily；不填时自动改用免密钥的 360 搜索 + OpenAlex，联网问答仍然可用 |
| `PAPEREAD_PORT` | 端口，默认 8010 |
| `PAPEREAD_DATA` | 数据目录，默认 `./data` |
| `PAPEREAD_MOCK=1` | 假翻译，用于验证流程不花钱 |

## 6. 常见问题

**Q：上传后一直显示"解析中"？**
大文件（>50MB 或扫描版 PDF）会慢一些；扫描版需要文字层，纯图片 PDF 无法解析。

**Q：翻译/导读/脉络都不动？**
检查右上角是否显示「未配置模型」。没配置时不会发任何请求。

**Q：端口被占用？**
`PAPEREAD_PORT=8123 ./start.sh`

**Q：数据存在哪？**
`./data/`：`library.json` 索引 + `papers/{id}.pdf|.blocks.json|.guide.json…`。
删掉整个 `data/` 即可重置。

## 7. 从源码构建前端（可选）

只有改了 `web/src` 才需要：

```bash
cd web
npm install
npm run build      # 产物在 web/dist，后端会自动托管
```

## 8. 开发

```bash
pip install -e ".[server,dev]"
pytest
python -m server.main          # 后端 + 打包后的前端
cd web && npm run dev          # 前端热更新（会代理 /api 到 8010）
```
