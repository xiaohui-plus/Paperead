$ErrorActionPreference = "Continue"
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

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

& $py -m PyInstaller packaging\paper-reflow-server.spec --noconfirm --clean --distpath dist-sidecar --workpath build-sidecar
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败" }

$exe = Join-Path $root "dist-sidecar\paper-reflow-server\paper-reflow-server.exe"
if (-not (Test-Path $exe)) { throw "没有产出 $exe" }

$size = [math]::Round(((Get-ChildItem (Split-Path $exe) -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Host "OK: $exe ($size MB)"

& (Join-Path $PSScriptRoot "stage-sidecar.ps1")
