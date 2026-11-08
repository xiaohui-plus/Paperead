$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$exe = Join-Path $root "dist-sidecar\paper-reflow-server\paper-reflow-server.exe"
if (-not (Test-Path $exe)) { throw "先运行 scripts\build-sidecar.ps1" }

$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
$listener.Start()
$port = ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
$listener.Stop()

$data = Join-Path $env:TEMP ("paper-reflow-smoke-" + [guid]::NewGuid().ToString("N"))
$token = "smoke-token"
New-Item -ItemType Directory -Force -Path $data | Out-Null

$py = Join-Path $root ".venv-build\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "缺少 .venv-build，无法生成测试 PDF" }
$sample = Join-Path $data "sample.pdf"
& $py -c "import sys, pymupdf; d=pymupdf.open(); p=d.new_page(); p.insert_text((72,72),'A Test Paper',fontsize=20); p.insert_text((72,110),'This is a test paragraph for paper-reflow.',fontsize=11); [p.insert_text((72,150+i*12), f'Line {i}: the quick brown fox jumps over the lazy dog.', fontsize=9) for i in range(100)]; d.save(sys.argv[1])" $sample
if ($LASTEXITCODE -ne 0) { throw "生成测试 PDF 失败" }

$env:PAPER_REFLOW_PORT = "$port"
$env:PAPER_REFLOW_DATA = $data
$env:PAPER_REFLOW_TOKEN = $token
$env:PAPER_REFLOW_MOCK = "1"

$proc = $null
$serverPid = $null
# 直接 Start-Process 一个 PyInstaller GUI exe 在部分 Windows 上会卡在
# CreateProcess/输入空闲等待；用 cmd start 启动，再按监听端口找 PID。
$launcher = Start-Process -FilePath "cmd.exe" `
    -ArgumentList '/c', "start `"`" `"$exe`"" -PassThru -WindowStyle Hidden
$launcher.WaitForExit(5000) | Out-Null
try {
    $ok = $false
    $health = $null
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Milliseconds 500
        try {
            $raw = & curl.exe -s --max-time 2 `
                -H "X-Paper-Reflow-Token: $token" "http://127.0.0.1:$port/api/health"
            if ($raw) {
                $health = $raw | ConvertFrom-Json
                if ($health.ok) { $ok = $true; break }
            }
        } catch {}
    }
    if (-not $ok) {
        $log = Join-Path $data "logs\server.log"
        if (Test-Path $log) { Get-Content $log -Tail 40 }
        throw "health 超时（端口 $port）"
    }

    $conn = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($conn) { $serverPid = $conn.OwningProcess }
    if (-not $serverPid) {
        $serverPid = (Get-Process paper-reflow-server -ErrorAction SilentlyContinue |
            Sort-Object StartTime -Descending | Select-Object -First 1).Id
    }

    if (-not $health.web) { throw "web/dist 未被打包" }
    if (-not $health.version) { throw "health 缺 version" }
    if (-not $health.frozen) { throw "health.frozen 应为 true" }

    $status = & curl.exe -s -o NUL -w "%{http_code}" --max-time 2 `
        "http://127.0.0.1:$port/api/papers"
    if ([int]$status -ne 403) { throw "未带 token 的请求应返回 403，实际 $status" }

    $cookieJar = Join-Path $data "cookies.txt"
    $pageFile = Join-Path $data "page.html"
    $pageCode = & curl.exe -s -o $pageFile -w "%{http_code}" -c $cookieJar --max-time 5 `
        "http://127.0.0.1:$port/"
    if ([int]$pageCode -ne 200) { throw "首页不是 200，实际 $pageCode" }
    if ((Get-Content $pageFile -Raw) -notmatch "paper-reflow") { throw "首页内容不对" }

    $papersCode = & curl.exe -s -o NUL -w "%{http_code}" -b $cookieJar --max-time 5 `
        "http://127.0.0.1:$port/api/papers"
    if ([int]$papersCode -ne 200) { throw "带 cookie 的 /api/papers 不是 200，实际 $papersCode" }

    $upload = & curl.exe -s -b $cookieJar --max-time 30 `
        -F "files=@$sample;type=application/pdf" "http://127.0.0.1:$port/api/papers/upload"
    $uploadJson = $upload | ConvertFrom-Json
    if (-not $uploadJson.added -or $uploadJson.added.Count -lt 1) {
        throw "上传失败：$upload"
    }
    $paperId = $uploadJson.added[0].id

    $ready = $false
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Milliseconds 500
        $list = & curl.exe -s -b $cookieJar --max-time 5 "http://127.0.0.1:$port/api/papers" |
            ConvertFrom-Json
        $row = $list.papers | Where-Object { $_.id -eq $paperId } | Select-Object -First 1
        if ($row.status -eq "ready") { $ready = $true; break }
        if ($row.status -eq "failed") { throw "解析失败：$($row.error)" }
    }
    if (-not $ready) { throw "解析超时（$paperId）" }

    $blocks = & curl.exe -s -b $cookieJar --max-time 10 `
        "http://127.0.0.1:$port/api/papers/$paperId/blocks" | ConvertFrom-Json
    if (-not $blocks.blocks -or $blocks.blocks.Count -lt 1) { throw "解析结果没有 blocks" }

    $bad = & curl.exe -s -o NUL -w "%{http_code}" -b $cookieJar --max-time 5 `
        -H "Origin: https://evil.example" "http://127.0.0.1:$port/api/papers"
    if ([int]$bad -ne 403) { throw "坏 Origin 应返回 403，实际 $bad" }

    Write-Host "SMOKE OK version=$($health.version) port=$port blocks=$($blocks.blocks.Count) data=$($health.data)"
} finally {
    if ($serverPid) {
        Stop-Process -Id $serverPid -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -Recurse -Force $data -ErrorAction SilentlyContinue
}
