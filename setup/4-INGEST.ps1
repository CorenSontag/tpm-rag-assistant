# Only needed if data\qdrant_storage did not come across.
# Requires the PDFs and Tesseract with Thai. Takes many minutes.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py   = Join-Path $root ".venv\Scripts\python.exe"
$cfg  = Join-Path $root "config\documents.json"

if (-not (Test-Path $py)) { Write-Host "Run 2-SETUP.ps1 first." -ForegroundColor Red; exit 1 }

if (-not (Test-Path $cfg)) {
    Copy-Item (Join-Path $root "config\documents.example.json") $cfg
    Write-Host "Created config\documents.json - edit the PDF paths, then re-run." -ForegroundColor Yellow
    Write-Host "Find them with:" -ForegroundColor Cyan
    Write-Host "  & '$py' -m tpm_rag.ingest --scan `"$env:USERPROFILE\Downloads`""
    exit 0
}

Push-Location (Join-Path $root "repos\tpm-chatbot-api")
try {
    Write-Host "`nDry run - checking paths and Tesseract..." -ForegroundColor Cyan
    & $py -m tpm_rag.ingest --config $cfg --dry-run
    if ($LASTEXITCODE -ne 0) { exit 1 }
    if ((Read-Host "`nProceed with the real ingest? [y/N]") -ne "y") { exit 0 }
    & $py -m tpm_rag.ingest --config $cfg
} finally { Pop-Location }
