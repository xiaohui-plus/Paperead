# 构建安卓 arm64 APK（内置前端 + 远程后端模式，不含研究与综述）
# 依赖：JDK 17、Android SDK/NDK、Rust aarch64-linux-android target
param(
  [string]$Target = "aarch64"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# 环境里可能残留失效路径（如 JAVA_HOME=D:\java），只有目录真实存在才采用
function Use-Dir([string]$value, [string]$fallback) {
  if ($value -and (Test-Path -LiteralPath $value)) { return $value }
  return $fallback
}

$jdk = Use-Dir $env:JAVA_HOME "F:\Agent\toolchains\jdk\jdk-17.0.21+9"
$sdk = Use-Dir $env:ANDROID_HOME "F:\Agent\toolchains\android-sdk"
$ndk = Use-Dir $env:NDK_HOME (Join-Path $sdk "ndk\27.2.12479018")
# 必须是纯 ASCII 路径：否则 Kotlin 编译 tauri-android 源码时中文用户名会被转义成乱码
$cargoHome = Use-Dir $env:CARGO_HOME "F:\Agent\toolchains\cargo-home"

$env:JAVA_HOME = $jdk
$env:ANDROID_HOME = $sdk
$env:ANDROID_SDK_ROOT = $sdk
$env:NDK_HOME = $ndk
$env:CARGO_HOME = $cargoHome
$env:PATH = "$jdk\bin;$sdk\platform-tools;$env:USERPROFILE\.cargo\bin;$env:PATH"
$env:TEMP = Join-Path $root ".tmp"
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Force $env:TEMP | Out-Null

$androidDir = Join-Path $root "src-tauri\gen\android"
if (-not (Test-Path $androidDir)) {
  npx tauri android init
  if ($LASTEXITCODE -ne 0) { throw "tauri android init 失败" }
}

# 首次构建自动生成签名密钥；请务必备份 keystore 与口令，后续升级必须使用同一密钥
if (-not (Test-Path (Join-Path $androidDir "keystore.properties"))) {
  $pass = -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 24 | ForEach-Object { [char]$_ })
  & "$jdk\bin\keytool.exe" -genkeypair -v `
    -keystore (Join-Path $androidDir "paperead-release.keystore") `
    -alias paperead -keyalg RSA -keysize 2048 -validity 10000 `
    -storepass $pass -keypass $pass `
    -dname "CN=Paperead, OU=Personal, O=Paperead, C=CN"
  if ($LASTEXITCODE -ne 0) { throw "生成签名密钥失败" }
  @(
    "storeFile=paperead-release.keystore"
    "storePassword=$pass"
    "keyAlias=paperead"
    "keyPassword=$pass"
  ) | Set-Content -Encoding ASCII (Join-Path $androidDir "keystore.properties")
  Write-Host "已生成签名密钥：$androidDir\paperead-release.keystore，请备份"
}

npx tauri android build --apk --target $Target
if ($LASTEXITCODE -ne 0) { throw "Android 构建失败" }

$version = (Get-Content (Join-Path $root "package.json") -Raw | ConvertFrom-Json).version
$built = Get-ChildItem (Join-Path $androidDir "app\build\outputs\apk") -Recurse -Filter "*.apk" |
  Where-Object { $_.Name -notlike "*-unsigned*" } |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 1
if (-not $built) { throw "未找到构建产物 APK" }

$outDir = Join-Path (Split-Path -Parent $root) "android"
New-Item -ItemType Directory -Force $outDir | Out-Null
$label = if ($Target -eq "aarch64") { "arm64" } else { $Target }
$outFile = Join-Path $outDir "Paperead-$version-$label.apk"
Copy-Item $built.FullName $outFile -Force
Write-Host "APK: $outFile ($([math]::Round((Get-Item $outFile).Length / 1MB, 2)) MB)"
