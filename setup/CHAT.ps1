# Talk to the bot in this terminal. No API, no browser, no CORS.
$root = Split-Path -Parent $PSScriptRoot
$api  = Join-Path $root "repos\tpm-chatbot-api"
$py   = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Write-Host "Run 2-SETUP.ps1 first." -ForegroundColor Red; exit 1 }
Push-Location $api
try { & $py cli.py } finally { Pop-Location }
