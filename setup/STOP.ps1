$root = Split-Path -Parent $PSScriptRoot
docker stop tpm-qdrant 2>&1 | Out-Null
Write-Host "Qdrant stopped." -ForegroundColor Green
Write-Host "Close the API and demo windows manually, or:" -ForegroundColor Yellow
Write-Host '  Get-Process python,uvicorn -ErrorAction SilentlyContinue | Stop-Process'
