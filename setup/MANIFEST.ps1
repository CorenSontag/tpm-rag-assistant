# Generate or verify MANIFEST.txt.
#
#   .\MANIFEST.ps1            verify; exits 1 on any mismatch
#   .\MANIFEST.ps1 -Write     regenerate
#
# Why this exists. MANIFEST.txt used to be written by hand and drifted until it
# was wrong about the two files it most warned about: it asserted a widget hash
# of 3118d129..., warned that 2d7da8cf... was stale, and the actual file matched
# neither. It claimed app/agent.py was b2a38b1f..., which turned out to be the
# hash of agent.py.orig, the pre-patch backup sitting next to it. Its header
# said "CODE ONLY - no vector index" while the bundle shipped 593 MB of index.
#
# An integrity file that is wrong is worse than no integrity file, because
# people act on it. The web team checksums the widget against this before
# deploying, so a stale entry rejects the correct file.
#
# File list comes from `git ls-files`, so it tracks .gitignore automatically and
# cannot quietly omit something the way a hand-maintained list can.

param([switch]$Write)

$ErrorActionPreference = "Stop"
$root     = Split-Path -Parent $PSScriptRoot
$manifest = Join-Path $root "MANIFEST.txt"

Push-Location $root
try {
    $files = @(git ls-files | Where-Object { $_ -and $_ -ne "MANIFEST.txt" } | Sort-Object)
    if (-not $files.Count) { throw "git ls-files returned nothing - is this a git repo?" }

    # Hash line-ending-normalised content, not raw bytes.
    #
    # git's core.autocrlf rewrites text files to CRLF on checkout on Windows, so
    # raw-byte hashes recorded in one working tree do not survive a clone into
    # another. That produced exactly the failure this file exists to prevent:
    # a fresh clone reported 66 files "changed", including the widget the header
    # tells the web team to checksum. Stripping CR before hashing makes the
    # manifest a property of the content rather than of the checkout.
    #
    # Binary files (detected by a NUL byte) are hashed raw - stripping 0x0D from
    # a PDF or a zip would be corruption, not normalisation.
    function Get-NormalizedHash([string]$path) {
        $bytes = [System.IO.File]::ReadAllBytes($path)
        $probe = [Math]::Min($bytes.Length, 8000)
        $isBinary = $false
        for ($i = 0; $i -lt $probe; $i++) { if ($bytes[$i] -eq 0) { $isBinary = $true; break } }
        if (-not $isBinary -and $bytes.Length) {
            $bytes = $bytes | Where-Object { $_ -ne 13 }
            if ($null -eq $bytes) { $bytes = @() }
        }
        $sha = [System.Security.Cryptography.SHA256]::Create()
        try {
            $h = $sha.ComputeHash([byte[]]$bytes)
        } finally { $sha.Dispose() }
        [PSCustomObject]@{
            Hash = ([System.BitConverter]::ToString($h) -replace '-', '').ToLower()
            Size = $bytes.Length
        }
    }

    $rows = foreach ($rel in $files) {
        $full = Join-Path $root $rel
        if (-not (Test-Path $full)) { continue }
        $n = Get-NormalizedHash $full
        [PSCustomObject]@{
            Hash = $n.Hash
            Size = $n.Size
            Path = $rel
        }
    }

    if ($Write) {
        $sb = [System.Text.StringBuilder]::new()
        [void]$sb.AppendLine("TPM-SYSTEM - file manifest")
        [void]$sb.AppendLine("generated $(Get-Date -Format 'yyyy-MM-dd HH:mm') local, by setup/MANIFEST.ps1")
        [void]$sb.AppendLine("$($rows.Count) files, from git ls-files")
        [void]$sb.AppendLine("")
        [void]$sb.AppendLine("Regenerate:  .\setup\MANIFEST.ps1 -Write")
        [void]$sb.AppendLine("Verify:      .\setup\MANIFEST.ps1        (exits 1 on mismatch)")
        [void]$sb.AppendLine("")
        [void]$sb.AppendLine("BUNDLE STATE: source only. The vector index lives in data/, which is")
        [void]$sb.AppendLine("gitignored and NOT covered by these hashes. Run setup/1-BUILD-BUNDLE.ps1")
        [void]$sb.AppendLine("on the source machine to produce a bundle carrying the index.")
        [void]$sb.AppendLine("")
        [void]$sb.AppendLine("The widget is checksummed by the web team before deploy. Its current hash:")
        $w = $rows | Where-Object { $_.Path -eq "repos/tpm-chat-widget/tpm-widget.js" }
        if ($w) { [void]$sb.AppendLine("  tpm-widget.js  $($w.Hash)") }
        [void]$sb.AppendLine("")
        [void]$sb.AppendLine(("=" * 78))
        [void]$sb.AppendLine("")
        foreach ($r in $rows) {
            [void]$sb.AppendLine(("{0}  {1,8}  {2}" -f $r.Hash, $r.Size, $r.Path))
        }
        [System.IO.File]::WriteAllText($manifest, $sb.ToString(), [System.Text.UTF8Encoding]::new($false))
        Write-Host "Wrote MANIFEST.txt - $($rows.Count) files" -ForegroundColor Green
        exit 0
    }

    # --- verify ---
    if (-not (Test-Path $manifest)) {
        Write-Host "MANIFEST.txt missing. Run: .\setup\MANIFEST.ps1 -Write" -ForegroundColor Red
        exit 1
    }

    $recorded = @{}
    foreach ($line in [System.IO.File]::ReadAllLines($manifest)) {
        if ($line -match '^([0-9a-f]{64})\s+(\d+)\s+(.+)$') { $recorded[$Matches[3].Trim()] = $Matches[1] }
    }
    if (-not $recorded.Count) { Write-Host "MANIFEST.txt has no hash rows." -ForegroundColor Red; exit 1 }

    $bad = @(); $missing = @(); $extra = @()
    foreach ($r in $rows) {
        if (-not $recorded.ContainsKey($r.Path)) { $extra += $r.Path }
        elseif ($recorded[$r.Path] -ne $r.Hash)  { $bad   += $r.Path }
    }
    $onDisk = $rows.Path
    foreach ($p in $recorded.Keys) { if ($onDisk -notcontains $p) { $missing += $p } }

    foreach ($p in $bad)     { Write-Host "CHANGED  $p" -ForegroundColor Red }
    foreach ($p in $missing) { Write-Host "MISSING  $p" -ForegroundColor Red }
    foreach ($p in $extra)   { Write-Host "UNLISTED $p" -ForegroundColor Yellow }

    if ($bad.Count -or $missing.Count -or $extra.Count) {
        Write-Host "`nMANIFEST MISMATCH: $($bad.Count) changed, $($missing.Count) missing, $($extra.Count) unlisted" -ForegroundColor Red
        Write-Host "If the changes are intended: .\setup\MANIFEST.ps1 -Write" -ForegroundColor Yellow
        exit 1
    }
    Write-Host "MANIFEST OK - $($rows.Count) files match" -ForegroundColor Green
    exit 0
} finally { Pop-Location }
