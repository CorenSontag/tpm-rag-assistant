# Run this ONCE on the TARGET machine, from inside the unzipped bundle.
# Every path is derived from this script's location - nothing to edit.
#
#   .\2-SETUP.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$api  = Join-Path $root "repos\tpm-chatbot-api"
$rag  = Join-Path $root "repos\tpm-rag"
$data = Join-Path $root "data\qdrant_storage"

function Ok($m)   { Write-Host "  [ok]   $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  [warn] $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "  [fail] $m" -ForegroundColor Red; exit 1 }

Write-Host "`n=== 1. Prerequisites ===" -ForegroundColor Cyan

$py = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $py) { Die "Python not found. Install 3.10+ and re-run." }
$v = (python -c "import sys; print('%d.%d' % sys.version_info[:2])")
Ok "Python $v"
if ([version]$v -lt [version]"3.10") { Die "Python 3.10+ required, found $v" }

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Die "Docker not found. Install Docker Desktop, start it, then re-run."
}
docker info 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { Die "Docker is installed but not running. Start Docker Desktop." }
Ok "Docker running"

if (Get-Command tesseract -ErrorAction SilentlyContinue) {
    $langs = tesseract --list-langs 2>&1
    if ($langs -match "tha") { Ok "Tesseract with Thai" } else { Warn "Tesseract found, no Thai data - only matters for re-ingesting" }
} else {
    Warn "Tesseract not found - only needed to re-ingest PDFs, not to run the bot"
}

Write-Host "`n=== 2. Python environment ===" -ForegroundColor Cyan
$venv = Join-Path $root ".venv"
if (-not (Test-Path $venv)) { python -m venv $venv; Ok "created .venv" } else { Ok ".venv exists" }
$pip = Join-Path $venv "Scripts\pip.exe"

Write-Host "  installing (a few minutes)..."
& $pip install --quiet --upgrade pip
# [ingest] on purpose. The OCR toolchain (PyMuPDF, pytesseract, Pillow,
# llama-index-core) is an optional extra since it was found to be most of a
# 919 MB production image - the Dockerfile installs tpm-rag WITHOUT it. A dev
# machine is the thing that re-ingests, so it needs the extra; drop it here and
# `tpm_rag.ingest` dies on `import fitz` instead.
& $pip install --quiet -e "$rag[ingest]"
if ($LASTEXITCODE -ne 0) { Die "failed installing tpm-rag" }
& $pip install --quiet -r (Join-Path $root "config\requirements-api.txt")
if ($LASTEXITCODE -ne 0) { Die "failed installing API dependencies" }
Ok "tpm-rag installed, API dependencies installed"

Write-Host "`n=== 3. Configuration ===" -ForegroundColor Cyan
$envFile = Join-Path $api ".env"
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $root "config\.env.template") $envFile
    Ok "created repos\tpm-chatbot-api\.env from the template"
} else { Ok ".env already present" }

if ((Get-Content $envFile -Raw) -match "sk-put-your-key-here") {
    Warn "OPENAI_API_KEY is still the placeholder"
    $k = Read-Host "Paste your OpenAI key now (or Enter to skip)"
    if ($k) {
        (Get-Content $envFile -Raw).Replace("sk-put-your-key-here", $k) |
            Set-Content $envFile -NoNewline -Encoding UTF8
        Ok "key written"
    } else { Warn "edit $envFile before starting" }
}

Write-Host "`n=== 4. Vector index ===" -ForegroundColor Cyan
if (-not (Test-Path $data)) {
    New-Item -ItemType Directory -Force -Path $data | Out-Null
    Warn "data\qdrant_storage is empty - the bot will find nothing"
    Warn "copy it from the source machine, or run 4-INGEST.ps1"
} else {
    $mb = [math]::Round((Get-ChildItem $data -Recurse -File | Measure-Object Length -Sum).Sum / 1MB, 0)
    if ($mb -lt 10) { Warn "index looks small ($mb MB) - expected around 369 MB" } else { Ok "index present ($mb MB)" }
}

Write-Host "`n=== 5. Starting Qdrant ===" -ForegroundColor Cyan

# Use the exact image the index was built with, if the snapshot recorded it.
# Untagged "qdrant/qdrant" means whatever latest resolves to today, which is
# how a working index stops loading after an unrelated docker pull.
$image = "qdrant/qdrant"
$infoFile = Join-Path $root "data\snapshot\SNAPSHOT-INFO.txt"
if (Test-Path $infoFile) {
    $line = Select-String -Path $infoFile -Pattern "^container image:\s*(.+)$"
    if ($line -and $line.Matches[0].Groups[1].Value.Trim() -match "\S") {
        $image = $line.Matches[0].Groups[1].Value.Trim()
        Ok "pinning Qdrant to $image (recorded with the snapshot)"
    }
}
if ($image -eq "qdrant/qdrant") {
    Warn "no recorded image tag - using latest. Pin it once you know the version."
}

docker rm -f tpm-qdrant 2>&1 | Out-Null
docker run -d --name tpm-qdrant -p 6333:6333 -v "${data}:/qdrant/storage" $image | Out-Null
Start-Sleep -Seconds 6

# Restore from a snapshot if one shipped and the collection is not already
# populated. Snapshots survive a version change; a raw storage folder may not.
$snapDir = Join-Path $root "data\snapshot"
$snap = Get-ChildItem $snapDir -Filter "*.snapshot" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($snap) {
    $need = $true
    try {
        $c = Invoke-RestMethod "http://localhost:6333/collections/docs" -TimeoutSec 10
        if ($c.result.points_count -gt 0) { $need = $false; Ok "collection already has $($c.result.points_count) points; snapshot not needed" }
    } catch { }
    if ($need) {
        Write-Host "  restoring from $($snap.Name)..."
        # Check the HTTP status. This was `| Out-Null` followed by an
        # unconditional Ok "snapshot restored" until 2026-11-09, so a failed
        # restore was reported as a success and the operator found out later,
        # from an empty collection, with nothing to point at.
        #
        # The failure is not hypothetical: a snapshot created against a Windows
        # bind mount is corrupt by construction (Qdrant writes zeros for any
        # file whose st_blocks is 0, which is every small file on a 9p mount)
        # and its upload returns HTTP 500 with a WAL deserialisation error.
        # See data\snapshot\SNAPSHOT-INFO.txt.
        $respFile = Join-Path $env:TEMP "qdrant-snapshot-restore.json"
        $code = curl.exe -s -o $respFile -w "%{http_code}" -X POST `
                 "http://localhost:6333/collections/docs/snapshots/upload?priority=snapshot" `
                 -H "Content-Type:multipart/form-data" -F "snapshot=@$($snap.FullName)"
        if ($code -ne "200") {
            if (Test-Path $respFile) { Write-Host ("  " + (Get-Content $respFile -Raw)) -ForegroundColor Red }
            Die "snapshot upload returned HTTP $code - the index was NOT restored. If this snapshot was produced by 1-BUILD-BUNDLE.ps1 on a Windows bind mount it is corrupt; restore data\snapshot\corpus.npz with setup\corpus-backup.py instead."
        }
        Start-Sleep -Seconds 5
        Ok "snapshot restored (HTTP 200)"
    }
}

try {
    $c = Invoke-RestMethod "http://localhost:6333/collections/docs" -TimeoutSec 10
    $n = $c.result.points_count
    if ($n -gt 1000) { Ok "collection loaded: $n points" }
    elseif ($n -lt 1764) { Warn "only $n points - expected 1764 (1415 books + 349 website). Check per-source counts before blaming the mount." }
    else { Warn "collection is empty" }
} catch {
    Warn "could not read the collection yet - it may still be starting"
}

Write-Host "`n=== Done ===" -ForegroundColor Green
Write-Host @"

  Start everything:   .\setup\3-START.ps1
  Stop everything:    .\setup\STOP.ps1
  Talk in a terminal: .\setup\CHAT.ps1

  Before going public, read docs\MOUNT_CHECKLIST.md - especially
  ALLOWED_ORIGINS and the OpenAI spend cap.
"@
