$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$src = Join-Path $root "dist-sidecar\paperead-server"
$dst = Join-Path $root "src-tauri\resources\paperead-server"

if (-not (Test-Path (Join-Path $src "paperead-server.exe"))) {
    throw "缺少 sidecar，请先运行 scripts\build-sidecar.ps1"
}

if (Test-Path $dst) { Remove-Item -Recurse -Force $dst }
New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null
Copy-Item -Recurse -Force $src $dst

$exe = Join-Path $dst "paperead-server.exe"
$size = [math]::Round(((Get-ChildItem $dst -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Host "STAGED: $exe ($size MB)"
