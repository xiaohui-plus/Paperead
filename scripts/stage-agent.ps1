$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$src = Join-Path $root "agent"
$dst = Join-Path $root "src-tauri\resources\agent"

if (-not (Test-Path (Join-Path $src "src\bin.mjs"))) {
    throw "缺少 agent/src/bin.mjs"
}
if (-not (Test-Path (Join-Path $src "node_modules"))) {
    throw "缺少 agent/node_modules，请先运行 pnpm install --prefix agent"
}

if (Test-Path $dst) { Remove-Item -Recurse -Force $dst }
New-Item -ItemType Directory -Force $dst | Out-Null

# 只带运行必需的目录：sessions / test / docs / .pnpm-store 都是开发产物，不进安装包。
foreach ($name in @("src", "bundle", "profiles", "skills", "node_modules")) {
    $from = Join-Path $src $name
    if (-not (Test-Path $from)) { continue }
    robocopy $from (Join-Path $dst $name) /E /XJ /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "复制 agent/$name 失败（robocopy $LASTEXITCODE）" }
}

foreach ($name in @("package.json", "cordis.yml", "minimal.yml", "pnpm-lock.yaml", "pnpm-workspace.yaml")) {
    $from = Join-Path $src $name
    if (Test-Path $from) { Copy-Item $from (Join-Path $dst $name) -Force }
}

# file: 依赖在 node_modules 里是 junction，绝对路径换机器就失效；改成真实副本。
foreach ($scope in @("@paperead", "@paper-reflow")) {
    $link = Join-Path $dst "node_modules\$scope\dsh-research"
    if (Test-Path $link) { Remove-Item -Recurse -Force $link }
    New-Item -ItemType Directory -Force $link | Out-Null
    Copy-Item -Recurse -Force (Join-Path $src "bundle\*") $link
}

# 安装用户机器不一定有 Node，内置一份（Rust 侧优先用 agent/node.exe）。
$node = (Get-Command node -ErrorAction SilentlyContinue).Source
if ($node) {
    Copy-Item $node (Join-Path $dst "node.exe") -Force
} else {
    Write-Warning "没有找到 node，安装版将依赖用户机器上的 Node"
}

$size = [math]::Round(((Get-ChildItem $dst -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Host "STAGED agent: $dst ($size MB)"

# robocopy 成功也返回 1/2，脚本必须以 0 退出供上层判断。
exit 0
