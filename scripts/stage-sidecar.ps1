$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$src = Join-Path $root "dist-sidecar\paper-reflow-server"
$dst = Join-Path $root "src-tauri\resources\paper-reflow-server"

if (-not (Test-Path (Join-Path $src "paper-reflow-server.exe"))) {
    throw "缺少 sidecar，请先运行 scripts\build-sidecar.ps1"
}

if (Test-Path $dst) { Remove-Item -Recurse -Force $dst }
New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null
Copy-Item -Recurse -Force $src $dst

$exe = Join-Path $dst "paper-reflow-server.exe"
$size = [math]::Round(((Get-ChildItem $dst -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Host "STAGED: $exe ($size MB)"
