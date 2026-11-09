# Start the research agent host in development.
#
#   $env:PAPEREAD_LLM_KEY = "sk-..."
#   .\agent\dev.ps1
#
# The profile lives at $DSH_HOME/profiles/research, so DSH_HOME is this folder.

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

$env:DSH_HOME = $here
# 研究 agent 默认使用 full 组合（skills / todo / compaction）；
# 只有传输层冒烟测试才显式设成 minimal。
$env:PAPEREAD_AGENT_COMPOSE = $env:PAPEREAD_AGENT_COMPOSE ?? "full"
# 兼容旧版变量名（0.1.0 及更早用 PAPER_REFLOW_*）；
# 未单独配置时复用问答模型的环境变量（PAPEREAD_API_KEY / PAPEREAD_BASE_URL）。
$env:PAPEREAD_LLM_KEY = $env:PAPEREAD_LLM_KEY ?? $env:PAPER_REFLOW_LLM_KEY ?? $env:PAPEREAD_API_KEY
$env:PAPEREAD_AGENT_PORT = $env:PAPEREAD_AGENT_PORT ?? "8011"
$env:PAPEREAD_AGENT_TOKEN = $env:PAPEREAD_AGENT_TOKEN ?? "dev-token"
$env:PAPEREAD_LLM_BASE = $env:PAPEREAD_LLM_BASE ?? $env:PAPEREAD_BASE_URL ?? "https://api.deepseek.com"
$env:PAPEREAD_MODEL = $env:PAPEREAD_MODEL ?? "deepseek-v4-flash"

if (-not $env:PAPEREAD_LLM_KEY -and -not $env:DEEPSEEK_API_KEY) {
  Write-Warning "PAPEREAD_LLM_KEY is empty; /health works but a real turn will fail with MISSING_CREDENTIAL."
}

Push-Location $here
try {
  node .\src\bin.mjs
} finally {
  Pop-Location
}
