$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$py = Join-Path $root ".venv-build\Scripts\python.exe"
if (-not (Test-Path $py)) {
    throw "缺少 .venv-build，先运行 uv venv .venv-build --python 3.12"
}

# sidecar 内嵌的是构建时的 web/dist，前端改过就必须重建，不能只在缺失时构建。
# 依赖已装好时不再 npm ci：开发中 Vite 会占用 esbuild.exe，重装会 EPERM。
Push-Location (Join-Path $root "web")
$code = 0
if (-not (Test-Path "node_modules")) {
    npm ci
    $code = $LASTEXITCODE
}
if ($code -eq 0) {
    npm run build
    $code = $LASTEXITCODE
}
Pop-Location
if ($code -ne 0) { throw "前端构建失败" }

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

& $py -m PyInstaller packaging\paperead-server.spec --noconfirm --clean --distpath dist-sidecar --workpath build-sidecar
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败" }

$exe = Join-Path $root "dist-sidecar\paperead-server\paperead-server.exe"
if (-not (Test-Path $exe)) { throw "没有产出 $exe" }

$size = [math]::Round(((Get-ChildItem (Split-Path $exe) -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Host "OK: $exe ($size MB)"

& (Join-Path $PSScriptRoot "stage-sidecar.ps1")
