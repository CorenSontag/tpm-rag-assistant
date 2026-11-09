# Proves the install, in the order that isolates failures.
#
# This script used to be unable to fail. $ErrorActionPreference was "Continue",
# neither python step's exit code was checked, and neither run_eval.py nor
# test_chat.py set one — so it could print 0/15 and 8/12 and still exit 0.
# Step 3 also exited 0 when the API was simply not running, reporting a missing
# service as a pass.
#
# It now propagates. Exit 0 means every step it ran actually passed.
#
#   .\VERIFY.ps1              full run
#   .\VERIFY.ps1 -SkipPaid    steps 1 and 3 only; no OpenAI spend

param([switch]$SkipPaid)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$api  = Join-Path $root "repos\tpm-chatbot-api"
$py   = Join-Path $root ".venv\Scripts\python.exe"

$failures = @()

# Step 0 runs first because it is free, takes a fifth of a second, and needs no
# Docker, no API and no key - so it is the only step that can run when the stack
# is down. It was not in this script at all: the gate ran the two suites that
# cost money and skipped the one that costs nothing, which meant a red offline
# suite could sit unnoticed while VERIFY reported on the expensive paths.
Write-Host "`n0. Offline suite (free, no services)" -ForegroundColor Cyan
$offline = @("test_agent_loop", "test_api", "test_run_eval", "test_tools_escalation", "test_pacing")
Push-Location $api; & $py -m unittest @offline; $code = $LASTEXITCODE; Pop-Location
if ($code -ne 0) {
    Write-Host "   FAILED (exit $code)" -ForegroundColor Red
    $failures += "offline suite"
} else {
    Write-Host "   OK" -ForegroundColor Green
}

Write-Host "`n1. Qdrant" -ForegroundColor Cyan
try {
    $c = Invoke-RestMethod "http://localhost:6333/collections/docs" -TimeoutSec 10
    $n = $c.result.points_count
    Write-Host "   points_count = $n" -ForegroundColor Green

    # Per-source, because a total that is low because one source is MISSING
    # looks identical to a total that is low because the index is ABSENT, and
    # they need completely different fixes. That distinction cost a day once.
    $bodyBooks = '{"filter":{"must":[{"key":"source","match":{"any":["tpm_lean_factory","tpm_collab","content_am"]}}]},"exact":true}'
    $bodySite  = '{"filter":{"must":[{"key":"source","match":{"value":"website"}}]},"exact":true}'
    $books = (Invoke-RestMethod "http://localhost:6333/collections/docs/points/count" -Method Post -Body $bodyBooks -ContentType "application/json" -TimeoutSec 10).result.count
    $site  = (Invoke-RestMethod "http://localhost:6333/collections/docs/points/count" -Method Post -Body $bodySite  -ContentType "application/json" -TimeoutSec 10).result.count
    Write-Host "   books = $books   website = $site" -ForegroundColor Green

    if ($books -eq 0) {
        Write-Host "   BOOKS MISSING - the three Thai PDFs are not in this collection." -ForegroundColor Red
        Write-Host "   The mount may be fine. Re-ingest: setup\4-INGEST.ps1" -ForegroundColor Red
        $failures += "book corpus absent"
    }
    if ($site -eq 0) {
        Write-Host "   website corpus absent - the web team ingested this; no tool here rebuilds it." -ForegroundColor Yellow
    }
} catch {
    Write-Host "   unreachable - run 3-START.ps1" -ForegroundColor Red
    exit 1
}

if (-not $SkipPaid) {
    Write-Host "`n2. Retrieval regression (books only)" -ForegroundColor Cyan
    Push-Location $api; & $py run_eval.py; $code = $LASTEXITCODE; Pop-Location
    if ($code -ne 0) { Write-Host "   FAILED (exit $code)" -ForegroundColor Red; $failures += "run_eval" }
} else {
    Write-Host "`n2. Retrieval regression - SKIPPED (-SkipPaid)" -ForegroundColor DarkGray
}

Write-Host "`n3. API health" -ForegroundColor Cyan
$apiUp = $false
try {
    $h = Invoke-RestMethod "http://localhost:8001/health" -TimeoutSec 10
    Write-Host "   ok=$($h.ok) points=$($h.points)" -ForegroundColor Green
    $apiUp = $true
} catch {
    # Previously `exit 0` here, which reported a missing API as a pass.
    Write-Host "   API not running - run 3-START.ps1" -ForegroundColor Red
    $failures += "API not running"
}

if ($apiUp -and -not $SkipPaid) {
    Write-Host "`n4. Agent behaviour" -ForegroundColor Cyan
    Push-Location $api; & $py test_chat.py; $code = $LASTEXITCODE; Pop-Location
    if ($code -ne 0) { Write-Host "   FAILED (exit $code)" -ForegroundColor Red; $failures += "test_chat" }
} elseif (-not $apiUp) {
    Write-Host "`n4. Agent behaviour - NOT RUN (API down)" -ForegroundColor Red
    $failures += "test_chat not run"
} else {
    Write-Host "`n4. Agent behaviour - SKIPPED (-SkipPaid)" -ForegroundColor DarkGray
}

Write-Host ""
if ($failures.Count) {
    # A check that could not run is a FAILED check, not a passed one.
    Write-Host "VERIFY FAILED: $($failures -join ', ')" -ForegroundColor Red
    exit 1
}
Write-Host "VERIFY PASSED" -ForegroundColor Green
exit 0
