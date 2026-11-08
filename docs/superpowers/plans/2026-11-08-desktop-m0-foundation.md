# paper-reflow 桌面版 M0（基础打包）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让现有 FastAPI 应用在 PyInstaller 打包后仍能正确找到前端资源、使用独立数据目录、接受壳传入的端口与 token，并产出一个可独立运行的 `paper-reflow-server.exe`。

**Architecture:** 新增 `server/runtime.py` 负责 frozen 路径、数据目录、token；`server/main.py` 改为使用它，并加 cookie/header 鉴权中间件；PyInstaller 用 onedir + noconsole 打包，spec 把 `web/dist` 作为数据目录带入。

**Tech Stack:** Python 3.12、FastAPI、uvicorn、PyMuPDF、PyInstaller、pytest、uv。

---

## 文件结构

- 新增 `server/runtime.py`：运行环境探测（frozen / 路径 / 数据目录 / token / allowed origins）。
- 修改 `server/main.py`：使用 runtime 模块；health 返回版本；鉴权中间件；`uvicorn.run(app, ...)`。
- 新增 `packaging/server_entry.py`：PyInstaller 入口。
- 新增 `packaging/paper-reflow-server.spec`：打包配置。
- 新增 `scripts/build-sidecar.ps1`：可复现构建。
- 新增 `scripts/smoke-sidecar.ps1`：启动 exe 并验证 health/鉴权/静态页。
- 新增 `tests/test_runtime.py`：路径与 token 单测。
- 修改 `pyproject.toml`、`web/package.json`、`README.md`、`.gitignore`、`.gitattributes`。
- 新增 `LICENSE`（AGPL-3.0）、`THIRD_PARTY_NOTICES.md`、`PRIVACY.md`。

---

### Task 1: 初始化仓库与 AGPL 元数据

**Files:**
- Create: `LICENSE`
- Create: `THIRD_PARTY_NOTICES.md`
- Create: `PRIVACY.md`
- Modify: `pyproject.toml`
- Modify: `web/package.json`
- Modify: `README.md`
- Modify: `.gitignore`

- [ ] **Step 1: 取 AGPL-3.0 原文**

Run:
```powershell
Invoke-WebRequest -Uri https://www.gnu.org/licenses/agpl-3.0.txt -OutFile LICENSE
Get-Content LICENSE -TotalCount 5
```
Expected: `GNU AFFERO GENERAL PUBLIC LICENSE` / `Version 3, 19 November 2007`.

- [ ] **Step 2: 改 pyproject 许可与 server 依赖**

```toml
license = { text = "AGPL-3.0-only" }
# [project.optional-dependencies]
server = [
    "fastapi>=0.110",
    "uvicorn>=0.29",
    "httpx>=0.27",
    "python-multipart>=0.0.9",
]
```

- [ ] **Step 3: 改 README 的 License 段与顶部说明**

把 `## License` 下的 `MIT` 改为 AGPL-3.0，并加一句：

```markdown
本项目以 AGPL-3.0 发布。桌面版安装包与源码见 GitHub Releases。
```

- [ ] **Step 4: 写第三方声明与隐私说明**

`THIRD_PARTY_NOTICES.md` 至少列出：PyMuPDF（AGPL-3.0）、KaTeX（MIT）、React（MIT）、FastAPI（MIT）、uvicorn（BSD-3-Clause）、Tauri（MIT/Apache-2.0）、Python（PSF-2.0）。

`PRIVACY.md` 写明：无遥测、PDF 本地处理、论文文本会发送给用户配置的模型服务商、联网检索可选。

- [ ] **Step 5: 更新 .gitignore**

追加：

```gitignore
# 桌面构建
.venv-build/
build-sidecar/
dist-sidecar/
src-tauri/target/
src-tauri/binaries/
src-tauri/gen/
*.exe
*.msi
*.nsis
*.zip
```

- [ ] **Step 6: `git init` 并提交**

```powershell
git init
git add .
git commit -m "chore: initialize repo with AGPL-3.0 and desktop build scaffolding"
```

若 `user.email` 未配置，先设置仓库级身份。

---

### Task 1.5: 建立干净构建环境

**Files:**
- Create: `.venv-build/`（git 忽略）
- Create: `requirements-build.txt`

- [ ] **Step 1: 建 venv**

Run:
```powershell
uv venv .venv-build --python 3.12
uv pip install --python .venv-build\Scripts\python.exe -e ".[server]" pytest "pyinstaller>=6.10"
```
Expected: `.venv-build\Scripts\python.exe` 存在。

- [ ] **Step 2: 验证关键依赖**

Run:
```powershell
.venv-build\Scripts\python.exe -c "import pymupdf, fastapi, uvicorn, multipart, certifi; print(pymupdf.__version__, fastapi.__version__)"
```
Expected: 打印版本号，无 ImportError。

- [ ] **Step 3: 记录构建依赖**

`requirements-build.txt`：

```text
# 仅用于打 sidecar，不进入运行时
pyinstaller>=6.10
```

- [ ] **Step 4: 提交**

```powershell
git add requirements-build.txt
git commit -m "build: document sidecar build dependencies"
```

---

### Task 2: 运行环境探测模块

**Files:**
- Create: `server/runtime.py`
- Test: `tests/test_runtime.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_runtime.py
import sys
from pathlib import Path

from server.runtime import allowed_origins, default_data_dir, ensure_data_dir, new_token, web_dist


def test_web_dist_uses_meipass_when_frozen(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert web_dist() == tmp_path / "web" / "dist"


def test_default_data_dir_prefers_env(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER_REFLOW_DATA", str(tmp_path / "lib"))
    assert default_data_dir() == (tmp_path / "lib").resolve()


def test_default_data_dir_uses_localappdata_when_frozen(monkeypatch, tmp_path):
    monkeypatch.delenv("PAPER_REFLOW_DATA", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "bundle"), raising=False)
    assert default_data_dir() == tmp_path / "paper-reflow"


def test_ensure_data_dir_creates_layout(tmp_path):
    ensure_data_dir(tmp_path)
    assert (tmp_path / "papers").is_dir()
    assert (tmp_path / "logs").is_dir()
    assert (tmp_path / "backups").is_dir()


def test_new_token_is_random():
    assert new_token() != new_token()
    assert len(new_token()) >= 32


def test_allowed_origins_include_loopback_and_dev():
    assert "http://127.0.0.1:8123" in allowed_origins(8123)
    assert "http://localhost:5173" in allowed_origins(8123)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv-build\Scripts\python.exe -m pytest tests/test_runtime.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'server.runtime'`.

- [ ] **Step 3: 实现 runtime.py**

```python
"""运行环境探测：frozen 路径、数据目录、token、允许的 Origin。"""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

APP_NAME = "paper-reflow"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_root() -> Path:
    """PyInstaller 解压目录；开发态为仓库根。"""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def web_dist() -> Path:
    return bundle_root() / "web" / "dist"


def default_data_dir() -> Path:
    env = os.getenv("PAPER_REFLOW_DATA")
    if env:
        return Path(env).expanduser().resolve()
    if is_frozen():
        base = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA")
        if base:
            return Path(base) / APP_NAME
        return Path.home() / f".{APP_NAME}"
    return bundle_root() / "data"


def ensure_data_dir(path: Path) -> Path:
    for child in ("papers", "logs", "backups"):
        (path / child).mkdir(parents=True, exist_ok=True)
    return path


def new_token() -> str:
    return secrets.token_urlsafe(32)


def allowed_origins(port: int) -> set[str]:
    return {
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    }
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv-build\Scripts\python.exe -m pytest tests/test_runtime.py -q`
Expected: `6 passed`.

- [ ] **Step 5: 提交**

```powershell
git add server/runtime.py tests/test_runtime.py
git commit -m "feat(server): add runtime path and data-dir detection"
```

---

### Task 3: server 接入 frozen 路径、token 与 health

**Files:**
- Modify: `server/main.py`

- [ ] **Step 1: 改导入与常量**

在 `server/main.py` 顶部加入：

```python
from fastapi import Request
from fastapi.responses import JSONResponse
from paper_reflow import __version__
from server.runtime import (
    allowed_origins,
    default_data_dir,
    ensure_data_dir,
    is_frozen,
    web_dist,
)
```

把：

```python
ROOT = Path(__file__).resolve().parent.parent
WEB_DIST = ROOT / "web" / "dist"
DATA_DIR = Path(os.getenv("PAPER_REFLOW_DATA", str(ROOT / "data")))
```

改为：

```python
WEB_DIST = web_dist()
DATA_DIR = ensure_data_dir(default_data_dir())
PORT = int(os.getenv("PAPER_REFLOW_PORT", "8010"))
TOKEN = os.getenv("PAPER_REFLOW_TOKEN", "")
ORIGINS = allowed_origins(PORT)
```

删除不再使用的 `import secrets`（token 由壳生成传入）。

- [ ] **Step 2: 加鉴权中间件**

在 `app.add_middleware(CORSMiddleware, ...)` 之后加入：

```python
@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api"):
        supplied = request.cookies.get("pr_token") or request.headers.get(
            "x-paper-reflow-token"
        )
        if TOKEN and supplied != TOKEN:
            return JSONResponse({"detail": "unauthorized"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin not in ORIGINS:
            return JSONResponse({"detail": "origin not allowed"}, status_code=403)
    response = await call_next(request)
    content_type = response.headers.get("content-type", "")
    if content_type.startswith("text/html") and TOKEN and "pr_token" not in request.cookies:
        response.set_cookie(
            "pr_token", TOKEN, httponly=True, samesite="strict", path="/"
        )
    return response
```

- [ ] **Step 3: health 返回版本与 frozen 状态**

```python
@app.get("/api/health")
async def health() -> dict:
    return {
        "ok": True,
        "mock": MOCK,
        "web": WEB_DIST.exists(),
        "version": __version__,
        "frozen": is_frozen(),
        "data": str(DATA_DIR),
    }
```

- [ ] **Step 4: 主入口改成对象调用，并写文件日志**

```python
def _setup_logging() -> None:
    from logging.handlers import RotatingFileHandler

    log_path = DATA_DIR / "logs" / "server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


def main() -> None:
    import uvicorn

    _setup_logging()
    log.info("paper-reflow %s 启动：http://127.0.0.1:%d（数据：%s）", __version__, PORT, DATA_DIR)
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_config=None, access_log=False)
```

- [ ] **Step 5: 跑现有测试**

Run: `.venv-build\Scripts\python.exe -m pytest -q`
Expected: 全绿。

- [ ] **Step 6: 提交**

```powershell
git add server/main.py
git commit -m "feat(server): support frozen paths, token auth and health metadata"
```

---

### Task 4: PyInstaller sidecar

**Files:**
- Create: `packaging/server_entry.py`
- Create: `packaging/paper-reflow-server.spec`
- Create: `packaging/version_info.txt`
- Create: `scripts/build-sidecar.ps1`

- [ ] **Step 1: 入口**

```python
# packaging/server_entry.py
from server.main import main

if __name__ == "__main__":
    main()
```

- [ ] **Step 2: spec**

```python
# packaging/paper-reflow-server.spec
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH).resolve().parent.parent
pymupdf_datas, pymupdf_bins, pymupdf_hidden = collect_all("pymupdf")

a = Analysis(
    [str(ROOT / "packaging" / "server_entry.py")],
    pathex=[str(ROOT)],
    binaries=pymupdf_bins,
    datas=[(str(ROOT / "web" / "dist"), "web/dist")] + pymupdf_datas,
    hiddenimports=pymupdf_hidden
    + [
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
        "fastapi",
        "multipart",
        "certifi",
    ],
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "PIL", "pytest", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="paper-reflow-server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="paper-reflow-server")
```

- [ ] **Step 3: 构建脚本**

```powershell
# scripts/build-sidecar.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$py = Join-Path $root ".venv-build\Scripts\python.exe"
if (-not (Test-Path $py)) {
    throw "缺少 .venv-build，先运行 uv venv .venv-build --python 3.12"
}

if (-not (Test-Path (Join-Path $root "web\dist\index.html"))) {
    Write-Host "web/dist 不存在，先构建前端"
    Push-Location (Join-Path $root "web")
    npm ci
    npm run build
    Pop-Location
}

& $py -m PyInstaller packaging\paper-reflow-server.spec --noconfirm --clean
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败" }

$exe = Join-Path $root "dist-sidecar\paper-reflow-server\paper-reflow-server.exe"
if (-not (Test-Path $exe)) { throw "没有产出 $exe" }
Write-Host "OK: $exe"
```

- [ ] **Step 4: 构建**

Run:
```powershell
.\scripts\build-sidecar.ps1
```
Expected: `OK: ...\dist-sidecar\paper-reflow-server\paper-reflow-server.exe`.

- [ ] **Step 5: 提交**

```powershell
git add packaging scripts
git commit -m "build: add PyInstaller sidecar spec and build script"
```

---

### Task 5: sidecar 冒烟测试

**Files:**
- Create: `scripts/smoke-sidecar.ps1`

- [ ] **Step 1: 写脚本**

```powershell
# scripts/smoke-sidecar.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$exe = Join-Path $root "dist-sidecar\paper-reflow-server\paper-reflow-server.exe"
$data = Join-Path $env:TEMP ("paper-reflow-smoke-" + [guid]::NewGuid().ToString("N"))
$port = 8123
$token = "smoke-token"

$env:PAPER_REFLOW_PORT = "$port"
$env:PAPER_REFLOW_DATA = $data
$env:PAPER_REFLOW_TOKEN = $token
$env:PAPER_REFLOW_MOCK = "1"

$proc = Start-Process -FilePath $exe -PassThru -WindowStyle Hidden
try {
    $ok = $false
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Milliseconds 500
        try {
            $r = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/health" -Headers @{ "X-Paper-Reflow-Token" = $token } -TimeoutSec 2
            if ($r.ok) { $ok = $true; break }
        } catch {}
    }
    if (-not $ok) { throw "health 超时" }
    if (-not $r.web) { throw "web/dist 未被打包" }
    if (-not $r.version) { throw "health 缺 version" }

    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/papers" -TimeoutSec 2 | Out-Null
        throw "未带 token 的请求不应成功"
    } catch {
        if ($_.Exception.Response.StatusCode.value__ -ne 403) { throw }
    }

    $page = Invoke-WebRequest -Uri "http://127.0.0.1:$port/" -UseBasicParsing -TimeoutSec 5
    if ($page.StatusCode -ne 200) { throw "首页不是 200" }
    if ($page.Content -notmatch "paper-reflow") { throw "首页内容不对" }

    Write-Host "SMOKE OK version=$($r.version) data=$($r.data)"
} finally {
    if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force }
    Remove-Item -Recurse -Force $data -ErrorAction SilentlyContinue
}
```

- [ ] **Step 2: 运行**

Run: `.\scripts\smoke-sidecar.ps1`
Expected: `SMOKE OK version=0.1.0 ...`.

- [ ] **Step 3: 检查残留进程**

Run: `Get-Process paper-reflow-server -ErrorAction SilentlyContinue`
Expected: 无输出。

- [ ] **Step 4: 提交**

```powershell
git add scripts/smoke-sidecar.ps1
git commit -m "test: add sidecar smoke test"
```

---

### Task 6: 全量验证

- [ ] **Step 1: 单测**

Run: `.venv-build\Scripts\python.exe -m pytest -q`
Expected: 全绿。

- [ ] **Step 2: 构建 + 冒烟**

Run:
```powershell
.\scripts\build-sidecar.ps1
.\scripts\smoke-sidecar.ps1
```
Expected: `SMOKE OK`.

- [ ] **Step 3: 查看产物大小**

Run: `Get-ChildItem dist-sidecar\paper-reflow-server | Measure-Object Length -Sum`
Expected: 80–150MB。

- [ ] **Step 4: 提交**

```powershell
git add .
git commit -m "chore: M0 foundation complete"
```
