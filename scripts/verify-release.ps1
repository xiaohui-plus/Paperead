$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$config = Get-Content "src-tauri\tauri.conf.json" -Raw | ConvertFrom-Json
$version = $config.version
$installer = Join-Path $root "release\Paperead_${version}_x64-setup.exe"
$apk = Join-Path $root "release\Paperead_${version}_android-arm64.apk"
$androidJson = Join-Path $root "release\android.json"
$sums = Join-Path $root "release\SHA256SUMS.txt"

$errors = @()
if (-not (Test-Path $installer)) { $errors += "缺少安装包：$installer" }
if (-not (Test-Path "$installer.sig")) { $errors += "缺少更新签名：$installer.sig" }
if (-not (Test-Path $apk)) { $errors += "缺少 APK：$apk" }
if (-not (Test-Path $androidJson)) {
    $errors += "缺少 Android 更新清单：$androidJson"
} else {
    $manifest = Get-Content $androidJson -Raw | ConvertFrom-Json
    if ($manifest.version -ne $version) { $errors += "android.json 版本 $($manifest.version) != $version" }
    if (Test-Path $apk) {
        $hash = (Get-FileHash $apk -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($manifest.sha256 -ne $hash) { $errors += "android.json 中 APK SHA256 不匹配" }
        if ($manifest.size -ne (Get-Item $apk).Length) { $errors += "android.json 中 APK 大小不匹配" }
    }
}
if (-not (Test-Path "src-tauri\resources\paperead-server\paperead-server.exe")) {
    $errors += "缺少已 stage 的 sidecar"
}
if (-not (Test-Path "src-tauri\resources\agent\src\bin.mjs")) {
    $errors += "缺少已 stage 的 agent 资源"
}
if (-not (Test-Path "src-tauri\resources\agent\node.exe")) {
    $errors += "agent 缺少内置 node.exe"
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
