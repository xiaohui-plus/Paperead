# Start the research agent host in development.
#
#   $env:PAPER_REFLOW_LLM_KEY = "sk-..."
#   .\agent\dev.ps1
#
# The profile lives at $DSH_HOME/profiles/research, so DSH_HOME is this folder.

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

$env:DSH_HOME = $here
$env:PAPER_REFLOW_AGENT_PORT = $env:PAPER_REFLOW_AGENT_PORT ?? "8011"
$env:PAPER_REFLOW_AGENT_TOKEN = $env:PAPER_REFLOW_AGENT_TOKEN ?? "dev-token"
$env:PAPER_REFLOW_LLM_BASE = $env:PAPER_REFLOW_LLM_BASE ?? "https://api.deepseek.com"
$env:PAPER_REFLOW_MODEL = $env:PAPER_REFLOW_MODEL ?? "deepseek-v4-flash"

if (-not $env:PAPER_REFLOW_LLM_KEY -and -not $env:DEEPSEEK_API_KEY) {
  Write-Warning "PAPER_REFLOW_LLM_KEY is empty; /health works but a real turn will fail with MISSING_CREDENTIAL."
}

Push-Location $here
try {
  node .\src\bin.mjs
} finally {
  Pop-Location
}
