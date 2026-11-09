# Run this on the SOURCE machine, once.
# Copies the vector index into the bundle and zips the whole thing.
#
#   .\1-BUILD-BUNDLE.ps1
#   .\1-BUILD-BUNDLE.ps1 -SourceRepo "D:\other\RAGProductionApp"

param(
    [string]$SourceRepo = "$env:USERPROFILE\Documents\RAGProductionApp",
    [switch]$SkipZip,
    [switch]$IncludeRawIndex,
    # Skip the confirmation before this script deletes .env and .venv from the
    # bundle root. See the "never ship the key" section at the end.
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

Write-Host "`nBundle root: $root" -ForegroundColor Cyan

# --- the index, as a snapshot ---------------------------------------------
# A Qdrant snapshot restores across server versions; a raw storage folder does
# not, and a subtly truncated copy loads without complaint then behaves as
# though random passages are missing. Snapshot is the safe path; the raw copy
# is kept only as a fallback.
Write-Host "`nTaking a Qdrant snapshot..." -ForegroundColor Cyan
$snapOk = $false
try {
    $r = Invoke-RestMethod -Method Post -Uri "http://localhost:6333/collections/docs/snapshots" -TimeoutSec 300
    $snapName = $r.result.name
    Write-Host "  created: $snapName"
    $dest = Join-Path $root "data\snapshot"
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    Invoke-WebRequest -Uri "http://localhost:6333/collections/docs/snapshots/$snapName" `
                      -OutFile (Join-Path $dest $snapName) -TimeoutSec 900
    $mb = [math]::Round((Get-Item (Join-Path $dest $snapName)).Length / 1MB, 1)
    Write-Host "  downloaded: $snapName ($mb MB)" -ForegroundColor Green

    # Is the snapshot actually restorable? A valid-looking tar is not enough.
    #
    # Incident, 2026-11-09: a snapshot taken exactly like the above would not
    # restore into a clean container of the identical Qdrant version. Qdrant's
    # snapshot writer treats st_blocks == 0 as "this file is one big hole" and
    # emits st_size zero bytes without reading it; Docker Desktop's 9p/drvfs
    # bind mount reports st_blocks == 0 for every small NTFS-resident file. So
    # every small JSON file inside the archive came out correct-length and
    # all-zero, and the upload failed with:
    #   Wal error: Can't init WAL: failed to read first-index file
    # The archive read cleanly under tar the whole time. Proven by snapshotting
    # the same collection off a NATIVE docker volume, which restores fine.
    #
    # Cheapest sufficient check: one known-small JSON member must not be zeros.
    # It needs no container and takes a second. If this fires, produce the
    # snapshot off a native volume instead (copy the storage dir into a docker
    # volume, run Qdrant on that, snapshot, copy out), or ship
    # data\snapshot\corpus.npz from setup\corpus-backup.py, which goes
    # through the HTTP API and is immune to this.
    $probe = tar -xOf (Join-Path $dest $snapName) "0/wal/first-index" 2>$null
    if (-not $probe -or ($probe -notmatch "\{")) {
        Write-Host "  snapshot member 0/wal/first-index is empty or zeroed" -ForegroundColor Red
        Write-Host "  -> this snapshot will NOT restore. Storage is on a bind mount." -ForegroundColor Red
        throw "refusing to ship a corrupt snapshot - see the comment above this check"
    }
    Write-Host "  integrity probe: 0/wal/first-index is real JSON" -ForegroundColor Green

    $ver = (Invoke-RestMethod "http://localhost:6333/" -TimeoutSec 10).version
    $img = docker inspect (docker ps --filter "ancestor=qdrant/qdrant" --format "{{.ID}}" | Select-Object -First 1) --format "{{.Config.Image}}" 2>$null
    @"
snapshot: $snapName
qdrant server version: $ver
container image: $img
taken: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz')
collection: docs
"@ | Set-Content (Join-Path $dest "SNAPSHOT-INFO.txt") -Encoding UTF8
    $snapOk = $true
} catch {
    Write-Host "  snapshot failed: $_" -ForegroundColor Yellow
    Write-Host "  Qdrant must be RUNNING for this. Falling back to a raw copy." -ForegroundColor Yellow
}

# --- the index, raw folder (fallback only) ---------------------------------
$src = Join-Path $SourceRepo "qdrant_storage"
if ($snapOk) {
    Write-Host "`nSnapshot succeeded - skipping the raw folder copy." -ForegroundColor Green
    Write-Host "Pass -IncludeRawIndex to include it anyway." -ForegroundColor DarkGray
    if (-not $IncludeRawIndex) { $src = "SKIP" }
}
if ($src -eq "SKIP") {
    # snapshot already captured
} elseif (-not (Test-Path $src)) {
    Write-Host "No qdrant_storage at $src" -ForegroundColor Yellow
    Write-Host "Pass -SourceRepo, or ship without it and re-ingest on the target."
} else {
    $running = docker ps --filter "ancestor=qdrant/qdrant" --format "{{.ID}}" 2>$null
    if ($running) {
        Write-Host "`nQdrant is running ($running)." -ForegroundColor Yellow
        Write-Host "It memory-maps these files; copying live can capture a torn state."
        $a = Read-Host "Stop it now? [Y/n]"
        if ($a -ne "n") { docker stop $running | Out-Null; Write-Host "  stopped" }
    }
    Write-Host "`nCopying the index (about 369 MB, takes a minute)..." -ForegroundColor Cyan
    robocopy $src (Join-Path $root "data\qdrant_storage") /E /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy failed ($LASTEXITCODE)" }
    Write-Host "  done"
}

# --- optional: the PDFs, only needed for re-ingesting ----------------------
Write-Host "`nInclude the source PDFs? Only needed if the target will re-ingest." -ForegroundColor Cyan
Write-Host "They are large (~367 MB) and the vector index already covers them."
$a = Read-Host "Include? [y/N]"
if ($a -eq "y") {
    $pdfDir = Read-Host "Folder containing the PDFs"
    if (Test-Path $pdfDir) {
        New-Item -ItemType Directory -Force -Path (Join-Path $root "data\pdfs") | Out-Null
        Copy-Item (Join-Path $pdfDir "*.pdf") (Join-Path $root "data\pdfs") -Force
        Write-Host "  copied"
    }
}

# --- never ship the key ----------------------------------------------------
# This deletes from $root IN PLACE, and $root is your working tree — not a copy.
# Run it on the tree you are developing in and it takes your API key and your
# virtualenv with it, with no prompt and nothing to undo. .env is gitignored by
# design, so version control cannot give it back either.
#
# So: back the secrets up outside the tree first, then confirm. Same instinct as
# reset.py:48 and purge.py:65-68, which make you type something before they
# destroy anything.

$envFiles = @(Get-ChildItem $root -Recurse -Force -Include ".env" -ErrorAction SilentlyContinue)
$venvDirs = @(Get-ChildItem $root -Recurse -Force -Include ".venv" -Directory -ErrorAction SilentlyContinue)

if ($envFiles.Count -or $venvDirs.Count) {
    Write-Host "`nAbout to DELETE from $root :" -ForegroundColor Yellow
    foreach ($f in $envFiles) { Write-Host "  .env   $($f.FullName)" -ForegroundColor Yellow }
    foreach ($d in $venvDirs) { Write-Host "  .venv  $($d.FullName)" -ForegroundColor Yellow }

    if ($envFiles.Count) {
        $backup = Join-Path (Split-Path -Parent $root) ("TPM-SECRETS-BACKUP-" + (Get-Date -Format "yyyy-MM-dd-HHmmss"))
        New-Item -ItemType Directory -Force -Path $backup | Out-Null
        foreach ($f in $envFiles) {
            $rel = $f.FullName.Substring($root.Length).TrimStart('\') -replace '\\', '__'
            Copy-Item $f.FullName (Join-Path $backup $rel) -Force
        }
        Write-Host "`n  .env backed up to: $backup" -ForegroundColor Green
        Write-Host "  That folder holds a live API key. Move it somewhere safe or delete it." -ForegroundColor Yellow
    }

    if (-not $Force) {
        Write-Host "`nThis is destructive and in-place. Pass -Force to skip this prompt." -ForegroundColor Yellow
        if ((Read-Host "Type the bundle folder name to confirm") -ne (Split-Path -Leaf $root)) {
            Write-Host "Name did not match - nothing deleted. Bundle left as-is." -ForegroundColor Red
            exit 1
        }
    }

    foreach ($f in $envFiles) {
        Remove-Item $f.FullName -Force
        Write-Host "removed $($f.FullName)" -ForegroundColor Yellow
    }
    foreach ($d in $venvDirs) { Remove-Item $d.FullName -Recurse -Force }
}

Get-ChildItem $root -Recurse -Force -Include "__pycache__" -Directory -ErrorAction SilentlyContinue |
    ForEach-Object { Remove-Item $_.FullName -Recurse -Force }

# --- zip -------------------------------------------------------------------
if (-not $SkipZip) {
    $zip = Join-Path (Split-Path -Parent $root) "TPM-SYSTEM.zip"
    if (Test-Path $zip) { Remove-Item $zip -Force }
    Write-Host "`nZipping..." -ForegroundColor Cyan
    Compress-Archive -Path $root -DestinationPath $zip
    $mb = [math]::Round((Get-Item $zip).Length / 1MB, 1)
    Write-Host "`nReady: $zip  ($mb MB)" -ForegroundColor Green
}

Write-Host "`nNo .env is included. The key goes in on the target machine." -ForegroundColor Yellow
