$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$config = Get-Content "src-tauri\tauri.conf.json" -Raw | ConvertFrom-Json
$version = $config.version
$installer = Join-Path $root "release\Paperead_${version}_x64-setup.exe"
$portable = Join-Path $root "release\Paperead_${version}_portable_x64.zip"
$sums = Join-Path $root "release\SHA256SUMS.txt"

$errors = @()
if (-not (Test-Path $installer)) { $errors += "缺少安装包：$installer" }
if (-not (Test-Path "$installer.sig")) { $errors += "缺少更新签名：$installer.sig" }
if (-not (Test-Path $portable)) { $errors += "缺少便携包：$portable" }
if (-not (Test-Path "src-tauri\resources\paperead-server\paperead-server.exe")) {
    $errors += "缺少已 stage 的 sidecar"
}

if (Test-Path $installer) {
    $actual = (Get-Item $installer).VersionInfo.FileVersion
    if ($actual -ne $version) { $errors += "安装包版本 $actual != $version" }
}

if (Test-Path $sums) {
    foreach ($line in Get-Content $sums) {
        if ($line -match '^([0-9A-Fa-f]{64})\s+(.+)$') {
            $expected = $Matches[1].ToUpperInvariant()
            $file = Join-Path $root ("release\" + $Matches[2].Trim())
            if (-not (Test-Path $file)) { $errors += "校验文件不存在：$file"; continue }
            $actual = (Get-FileHash $file -Algorithm SHA256).Hash
            if ($actual -ne $expected) { $errors += "SHA256 不匹配：$($Matches[2])" }
        }
    }
}

& (Join-Path $root ".venv-build\Scripts\python.exe") -m pytest -q
if ($LASTEXITCODE -ne 0) { $errors += "pytest 失败" }

if ($errors.Count -gt 0) {
    $errors | ForEach-Object { Write-Host "FAIL: $_" }
    exit 1
}
Write-Host "RELEASE OK: $version"
