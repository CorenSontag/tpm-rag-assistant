# Starts all three services, each in its own window.
#   .\3-START.ps1            API + Qdrant
#   .\3-START.ps1 -WithDemo  also serves the demo page on 5173

param([switch]$WithDemo)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$api  = Join-Path $root "repos\tpm-chatbot-api"
$data = Join-Path $root "data\qdrant_storage"
$venv = Join-Path $root ".venv\Scripts"

if (-not (Test-Path $venv)) { Write-Host "Run 2-SETUP.ps1 first." -ForegroundColor Red; exit 1 }

if (-not (docker ps --filter "name=tpm-qdrant" --format "{{.ID}}")) {
    Write-Host "Starting Qdrant..." -ForegroundColor Cyan
    docker rm -f tpm-qdrant 2>&1 | Out-Null
    $image = "qdrant/qdrant"
    $infoFile = Join-Path $root "data\snapshot\SNAPSHOT-INFO.txt"
    if (Test-Path $infoFile) {
        $line = Select-String -Path $infoFile -Pattern "^container image:\s*(.+)$"
        if ($line -and $line.Matches[0].Groups[1].Value.Trim() -match "\S") {
            $image = $line.Matches[0].Groups[1].Value.Trim()
        }
    }
    docker run -d --name tpm-qdrant -p 6333:6333 -v "${data}:/qdrant/storage" $image | Out-Null
    Start-Sleep -Seconds 5
}
Write-Host "Qdrant on :6333" -ForegroundColor Green

Start-Process powershell -ArgumentList @(
    "-NoExit","-Command",
    "cd '$api'; & '$venv\uvicorn.exe' app.api:app --reload --port 8001"
)
Write-Host "API on :8001" -ForegroundColor Green

if ($WithDemo) {
    $widget = Join-Path $root "repos\tpm-chat-widget"
    Start-Process powershell -ArgumentList @(
        "-NoExit","-Command","cd '$widget'; & '$venv\python.exe' -m http.server 5173"
    )
    Write-Host "Demo on http://localhost:5173/demo.html" -ForegroundColor Green
}

Write-Host "`nHealth: curl.exe http://localhost:8001/health" -ForegroundColor Cyan
