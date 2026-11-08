# 第三方组件与许可

paper-reflow 以 AGPL-3.0 发布。桌面安装包内含以下第三方组件，特此声明。

| 组件 | 用途 | 许可证 | 链接 |
|---|---|---|---|
| PyMuPDF / MuPDF | PDF 解析、渲染、表格识别 | AGPL-3.0（或商业授权） | https://pymupdf.readthedocs.io/ |
| Python | 运行时 | PSF-2.0 | https://www.python.org/ |
| FastAPI | 本地 HTTP 服务 | MIT | https://fastapi.tiangolo.com/ |
| Starlette / uvicorn | ASGI 服务 | BSD-3-Clause | https://www.uvicorn.org/ |
| pydantic | 请求模型 | MIT | https://docs.pydantic.dev/ |
| httpx | HTTP 客户端 | BSD-3-Clause | https://www.python-httpx.org/ |
| OpenAI Python SDK | 兼容模型接口 | Apache-2.0 | https://github.com/openai/openai-python |
| python-multipart | 文件上传解析 | Apache-2.0 | https://github.com/Kludex/python-multipart |
| Tauri | 桌面壳 | MIT / Apache-2.0 | https://tauri.app/ |
| React / React DOM | 前端 | MIT | https://react.dev/ |
| Vite | 前端构建 | MIT | https://vitejs.dev/ |
| KaTeX | 公式渲染 | MIT | https://katex.org/ |

## AGPL 说明

PyMuPDF 采用 AGPL-3.0 与商业授权双许可。本项目按 AGPL-3.0 开源分发，源码与构建
脚本见仓库根目录及对应 Release 的源码 tag。

如果你希望以闭源方式分发包含 PyMuPDF 的版本，需要向 Artifex 购买商业授权，或替换
PDF 引擎。详细条款以各组件官方许可证原文为准。
