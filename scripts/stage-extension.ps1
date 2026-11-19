$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$ext = Join-Path $root "extensions\browser"
$dst = Join-Path $root "src-tauri\resources\browser-extension"

if (-not (Test-Path (Join-Path $ext "node_modules"))) {
    throw "缺少 extensions/browser/node_modules，请先运行 npm --prefix extensions/browser install"
}

Push-Location $ext
try {
    npm run build
    if ($LASTEXITCODE -ne 0) { throw "Chromium 扩展构建失败" }
    npm run build:firefox
    if ($LASTEXITCODE -ne 0) { throw "Firefox 扩展构建失败" }
} finally {
    Pop-Location
}

if (Test-Path $dst) { Remove-Item -Recurse -Force $dst }
New-Item -ItemType Directory -Force $dst | Out-Null

foreach ($flavor in @("chrome-mv3", "firefox-mv2")) {
    $from = Join-Path $ext ".output\$flavor"
    if (-not (Test-Path (Join-Path $from "manifest.json"))) {
        throw "缺少扩展产物 $flavor，构建没有生成 manifest.json"
    }
    robocopy $from (Join-Path $dst $flavor) /E /XJ /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "复制扩展 $flavor 失败（robocopy $LASTEXITCODE）" }
}

$size = [math]::Round(((Get-ChildItem $dst -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 2)
Write-Host "扩展资源已就绪：$dst（$size MB，chrome-mv3 + firefox-mv2）"
# robocopy 成功也返回 1/2，脚本必须以 0 退出供上层判断。
exit 0