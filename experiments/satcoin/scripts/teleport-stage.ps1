# teleport-stage.ps1
#
# Run on the SOURCE PC (Triton) to stage a satcoin-experiment snapshot to Google
# Drive so another PC can rehydrate it in ~30 minutes instead of the 1-3 days a
# fresh bitcoind sync would take.
#
# What gets uploaded to infinityze:teleport/satcoin/:
#   - cgeno.exe                       (built cgen binary -- gitignored)
#   - tools/cryptominisat/cryptominisat5.exe  (solver binary -- gitignored)
#   - bitcoin.conf                    (RPC creds + prune/wallet settings)
#   - wallets/satcoin/                (the local Bitcoin Core wallet)
#   - blocks/ + chainstate/           (the pruned node datadir, ~10 GB)
#
# bitcoind is stopped cleanly for the duration of the upload (~30 min at typical
# home-broadband upload speed for 10 GB) then restarted via launch_bitcoind.ps1.
#
# Idempotent; safe to re-run. Preserves the local state.
#
# Flags:
#   -DryRun     Show what would be uploaded, don't actually rclone or stop bitcoind.
#   -SkipStop   Assume bitcoind is already stopped (don't try to stop or restart).
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\teleport-stage.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\teleport-stage.ps1 -DryRun

[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$SkipStop
)

$ErrorActionPreference = "Continue"
$RepoRoot   = Resolve-Path (Join-Path $PSScriptRoot "..\..\..")
$BitcoinDir = "C:\Users\dizzyvinci\AppData\Roaming\Bitcoin"
$RemoteRoot = "infinityze:teleport/satcoin"
$Launcher   = Join-Path $PSScriptRoot "launch_bitcoind.ps1"
$Stopper    = Join-Path $PSScriptRoot "stop_bitcoind.ps1"

function Log { param([string]$m) "[$(Get-Date -Format 'HH:mm:ss')] $m" }

Log "=== teleport-stage start ==="
Log "  repo:      $RepoRoot"
Log "  datadir:   $BitcoinDir"
Log "  remote:    $RemoteRoot"
Log "  dry-run:   $DryRun"

# Sanity: rclone available?
$rclone = Get-Command rclone -ErrorAction SilentlyContinue
if (-not $rclone) { Log "FATAL: rclone not on PATH. Install rclone + set up the infinityze remote (see claude-sync repo)."; exit 2 }

# Sanity: infinityze remote configured?
$remoteOk = rclone listremotes 2>$null | Select-String -Pattern '^infinityze:$'
if (-not $remoteOk) { Log "FATAL: infinityze: rclone remote not configured. rclone config."; exit 2 }

# Sanity: source files present?
$cgeno = Join-Path $RepoRoot "cgeno.exe"
$cms   = Join-Path $RepoRoot "tools\cryptominisat\cryptominisat5.exe"
$conf  = Join-Path $BitcoinDir "bitcoin.conf"
foreach ($f in @($cgeno, $cms, $conf)) {
    if (-not (Test-Path $f)) { Log "FATAL: missing source file: $f"; exit 2 }
}

# Stop bitcoind cleanly so the copied datadir isn't mid-flush.
if (-not $SkipStop -and -not $DryRun) {
    Log "stopping bitcoind cleanly via stop_bitcoind.ps1..."
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Stopper | Out-Null
    Start-Sleep -Seconds 3
} elseif ($SkipStop) {
    Log "SkipStop set -- assuming bitcoind already stopped"
} else {
    Log "DryRun -- would stop bitcoind here"
}

# Common rclone options.
$rcloneArgs = @("--progress","--transfers=8","--checkers=16","--fast-list")
if ($DryRun) { $rcloneArgs += "--dry-run" }

function Send { param([string]$src,[string]$dst,[string[]]$extra=@())
    if (-not (Test-Path $src)) { Log "  skip (not found): $src"; return }
    Log "  -> $dst"
    if ((Get-Item $src).PSIsContainer) {
        & rclone copy $src "${RemoteRoot}/$dst" @rcloneArgs @extra
    } else {
        & rclone copyto $src "${RemoteRoot}/$dst" @rcloneArgs
    }
}

# 1. Built binaries (small, fast)
Send $cgeno "binaries/cgeno.exe"
Send $cms   "binaries/cryptominisat5.exe"

# 2. Bitcoin config + wallet (small)
Send $conf  "datadir/bitcoin.conf"
Send (Join-Path $BitcoinDir "satcoin") "datadir/satcoin"

# 3. Pruned blocks + chainstate (~10 GB -- this is the long one)
Send (Join-Path $BitcoinDir "blocks")     "datadir/blocks"      @("--exclude=blocks_index/LOG*")
Send (Join-Path $BitcoinDir "chainstate") "datadir/chainstate"  @("--exclude=LOG*")

# Restart bitcoind
if (-not $SkipStop -and -not $DryRun) {
    Log "restarting bitcoind via launch_bitcoind.ps1..."
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Launcher | Out-Null
} elseif ($DryRun) {
    Log "DryRun -- would restart bitcoind here"
}

# Report final layout on Drive
Log "listing remote (top-level):"
& rclone lsd "$RemoteRoot" 2>&1 | ForEach-Object { Log "    $_" }

Log "=== teleport-stage done ==="
Log ""
Log "On the DESTINATION PC, run:"
Log "  powershell -ExecutionPolicy Bypass -File scripts\teleport-fetch.ps1"
