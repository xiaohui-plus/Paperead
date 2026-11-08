# 桌面版（Windows）构建与发布

桌面版是 **Tauri 2 壳 + Python FastAPI sidecar**：窗口加载
`http://127.0.0.1:<随机端口>`，sidecar 由 PyInstaller 打包，用户不需要安装
Python 或 Node。

## 下载与安装

安装包发布在 GitHub Releases：

- 安装版：`paper-reflow_<version>_x64-setup.exe`
- 安装包 ZIP（.exe 被拦截时用）：`paper-reflow_<version>_x64-setup.zip`
- 便携版：`paper-reflow_<version>_portable_x64.zip`
- 系统：Windows 10/11 x64
- 国内镜像：`https://ghproxy.net/https://github.com/xiaohui-plus/Paperead/releases/latest`
- 安装方式：per-user（安装目录 `%LOCALAPPDATA%\paper-reflow`），不弹 UAC
- 安装版数据目录：`%LOCALAPPDATA%\paper-reflow-data`（与安装目录分开，卸载/升级不影响文献库）
- 便携版：解压后双击 `启动.cmd`，数据在同级 `data\`；更新时下载新 ZIP，保留 `data\`
- 卸载：默认保留数据；可在应用内打开数据目录手动删除

首次启动会说明本地数据与模型请求范围，然后打开“模型与密钥”设置：回答模型负责问答/翻译，
识图模型负责论文里的图片识别，两者可以分别配置，推荐模型旁有直达官网的「获取 API Key」按钮。

## 从源码构建

前置：Windows 10/11 x64、Rust（MSVC toolchain）、VS Build Tools、Node 22+、
Python 3.12、uv。

```powershell
npm ci
npm ci --prefix web
uv venv .venv-build --python 3.12
uv pip install --python .venv-build\Scripts\python.exe -e ".[server]" "pyinstaller>=6.10"
npm run build:installer
npm run build:portable
npm run verify:release
```

产物：

```
src-tauri\target\release\bundle\nsis\paper-reflow_0.1.0_x64-setup.exe
release\paper-reflow_0.1.0_portable_x64.zip
release\paper-reflow_0.1.0_source.zip        # AGPL 对应源码
```

构建脚本会：

1. 构建 `web/dist`；
2. 用 PyInstaller 产出 `dist-sidecar\paper-reflow-server`（onedir，约 80MB）；
3. 复制到 `src-tauri\resources\paper-reflow-server`；
4. 编译 Tauri release 并生成 NSIS 安装包。

只改前端时也要重新打包 sidecar，因为 sidecar 托管的是打包时的 `web/dist`。
`npm run build:installer` 会按顺序处理。

开发调试：

```powershell
npm run build:sidecar
npm run tauri -- dev
```

## 自动更新

应用内自动更新面向**安装版**。便携版请下载新版便携 ZIP，覆盖 `paper-reflow-desktop.exe`
和 `resources\`，保留 `data\`。

当前 `src-tauri/tauri.conf.json` 的更新地址指向本仓库：

```json
"endpoints": [
  "https://github.com/xiaohui-plus/Paperead/releases/latest/download/latest.json"
]
```

如果 fork 到别的仓库，替换成你的实际地址（GitHub Releases 或对象存储/自定义域名）。
国内用户建议把 `latest.json` 与安装包放到可达的 CDN。

本地联调或自建更新源时，可以用环境变量覆盖配置里的地址（不用重新打包）：

```powershell
$env:PAPER_REFLOW_UPDATE_ENDPOINT = "http://127.0.0.1:8000/latest.json"
```

生成更新清单（GitHub Actions 里的 tauri-action 也会自动生成）：

```powershell
python scripts/make_latest_json.py `
  --version 0.1.1 `
  --installer release\paper-reflow_0.1.1_x64-setup.exe `
  --url https://dl.example.com/paper-reflow_0.1.1_x64-setup.exe `
  --out release\latest.json
```

`latest.json` 的 `signature` 来自同名 `.sig` 文件，必须由 `tauri.conf.json`
里的公钥对应的私钥签名。

生成更新签名密钥：

```powershell
npx tauri signer generate -w .tauri/updater.key
```

- 公钥写入 `src-tauri/tauri.conf.json` 的 `plugins.updater.pubkey`；
- 私钥和密码放入 GitHub Secrets：
  - `TAURI_SIGNING_PRIVATE_KEY`
  - `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`
- 私钥务必离线备份。丢失后老用户无法再收到自动更新。

本仓库已经生成了发布密钥：私钥在本机 `.tauri/updater.prod.key`（已 gitignore），
密码在 `.tauri/updater.prod.password`；同时写入 GitHub Actions Secrets。请把这两个
文件离线备份，不要提交到仓库。公钥已经在 `src-tauri/tauri.conf.json` 里。

## 发布流程

1. 改版本号：

   ```powershell
   python scripts/set_version.py 0.1.1
   ```

2. 提交并打 tag：

   ```powershell
   git add -A
   git commit -m "release: v0.1.1"
   git tag v0.1.1
   git push origin main --tags
   ```

3. `.github/workflows/release.yml` 会在 `windows-latest` 上：
   - 跑后端测试；
   - 构建前端与 sidecar；
   - 用 Tauri 生成 NSIS 安装包、`.sig` 和 `latest.json`；
   - 创建 **draft** Release。

4. 在 GitHub 上检查安装包和更新清单，然后发布 Release。

## 代码签名

未签名的 Windows 安装包会触发 SmartScreen。正式分发建议：

- 开源项目申请 SignPath 等免费 OSS 签名；或
- 购买 OV/EV 代码签名证书，在 CI 中接入 `signtool`/Tauri 的签名命令。

## 已知限制

- 打印：前端调用 `window.print()`，WebView2 下已验证会打开打印预览；若某些
  机器上不可用，后续改为后端生成 PDF。
- 安装被拦截：部分安全软件（如联想/火绒）会阻止未签名安装器把 exe 写入
  `%LOCALAPPDATA%`，报“无法打开要写入的文件”。将安装包/安装目录加入白名单，
  或使用便携 ZIP；正式版应做代码签名。
- 首发仅 Windows 10/11 x64；macOS 需要 Developer ID + 公证。
- 表格使用 PyMuPDF 内置识别（未打包可选的 ONNX layout 模型），复杂表格
  可能退化为裁图。
- 自动更新依赖签名密钥和可达的更新地址；未配置时只会在日志里记录失败，
  不影响正常使用。

## 许可

AGPL-3.0。第三方组件见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。
