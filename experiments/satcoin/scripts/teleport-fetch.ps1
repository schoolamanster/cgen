# teleport-fetch.ps1
#
# Run on the DESTINATION PC (Poseidon) to rehydrate a satcoin-experiment
# environment that was previously staged by teleport-stage.ps1 on Triton.
#
# After this finishes you'll have:
#   - cgeno.exe                       in the repo root
#   - tools/cryptominisat/cryptominisat5.exe
#   - %APPDATA%\Bitcoin\bitcoin.conf  (Triton's -- regenerate rpcpassword if you want)
#   - %APPDATA%\Bitcoin\wallets\satcoin\
#   - %APPDATA%\Bitcoin\blocks + chainstate  (pruned, ~10 GB)
# and bitcoind will be running via launch_bitcoind.ps1.
#
# Prereqs on Poseidon (see BOOTSTRAP.md for full details):
#   - The repo cloned at C:\Users\dizzyvinci\cgen_fetchredeem_optimization\
#     (`git clone git@github-dizzyvinci:dizzyvinci/cgen_fetchredeem_optimization.git`)
#   - Bitcoin Core 31.0 installed at C:\Program Files\Bitcoin\bitcoin-31.0\
#   - rclone with `infinityze:` remote configured (per POSEIDON_SETUP.md)
#   - Python 3.10+ on PATH
#
# Flags:
#   -DryRun     Show what would be downloaded without touching anything.
#   -SkipStart  Fetch files, but don't launch bitcoind afterwards.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\teleport-fetch.ps1

[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$SkipStart
)

$ErrorActionPreference = "Continue"
$RepoRoot   = Resolve-Path (Join-Path $PSScriptRoot "..\..\..")
$BitcoinDir = "C:\Users\dizzyvinci\AppData\Roaming\Bitcoin"
$RemoteRoot = "infinityze:teleport/satcoin"
$Launcher   = Join-Path $PSScriptRoot "launch_bitcoind.ps1"

function Log { param([string]$m) "[$(Get-Date -Format 'HH:mm:ss')] $m" }

Log "=== teleport-fetch start ==="
Log "  repo:      $RepoRoot"
Log "  datadir:   $BitcoinDir"
Log "  remote:    $RemoteRoot"
Log "  dry-run:   $DryRun"

# Sanity: rclone available?
$rclone = Get-Command rclone -ErrorAction SilentlyContinue
if (-not $rclone) { Log "FATAL: rclone not on PATH."; exit 2 }
$remoteOk = rclone listremotes 2>$null | Select-String -Pattern '^infinityze:$'
if (-not $remoteOk) { Log "FATAL: infinityze: rclone remote not configured."; exit 2 }

# Sanity: the remote bundle exists?
$stagedFiles = rclone lsf "$RemoteRoot" 2>$null
if (-not $stagedFiles) { Log "FATAL: nothing at $RemoteRoot. Run teleport-stage.ps1 on Triton first."; exit 2 }
Log "  found staged bundle on remote"

# Make sure bitcoind isn't running here (would fight with our incoming datadir)
$existing = Get-Process bitcoind -ErrorAction SilentlyContinue
if ($existing) {
    if ($DryRun) {
        Log "DryRun -- would stop existing bitcoind PID $($existing.Id)"
    } else {
        Log "stopping local bitcoind PID $($existing.Id) so we can replace its datadir"
        $existing | Stop-Process -Force
        Start-Sleep -Seconds 3
    }
}

# Common rclone options.
$rcloneArgs = @("--progress","--transfers=8","--checkers=16","--fast-list")
if ($DryRun) { $rcloneArgs += "--dry-run" }

function Fetch { param([string]$src,[string]$dst)
    $parent = Split-Path $dst -Parent
    if (-not $DryRun -and -not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
    Log "  <- $src -> $dst"
    if ($src -match "\.exe$" -or $src -match "\.conf$") {
        & rclone copyto "${RemoteRoot}/$src" $dst @rcloneArgs
    } else {
        & rclone copy "${RemoteRoot}/$src" $dst @rcloneArgs
    }
}

# 1. Binaries
Fetch "binaries/cgeno.exe"          (Join-Path $RepoRoot "cgeno.exe")
Fetch "binaries/cryptominisat5.exe" (Join-Path $RepoRoot "tools\cryptominisat\cryptominisat5.exe")

# 2. Bitcoin config + wallet
if (-not $DryRun -and -not (Test-Path $BitcoinDir)) { New-Item -ItemType Directory -Path $BitcoinDir -Force | Out-Null }
Fetch "datadir/bitcoin.conf" (Join-Path $BitcoinDir "bitcoin.conf")
Fetch "datadir/satcoin" (Join-Path $BitcoinDir "satcoin")

# 3. Pruned datadir (~10 GB, the long one)
Fetch "datadir/blocks"     (Join-Path $BitcoinDir "blocks")
Fetch "datadir/chainstate" (Join-Path $BitcoinDir "chainstate")

# Kick off bitcoind
if (-not $SkipStart -and -not $DryRun) {
    Log "launching bitcoind via launch_bitcoind.ps1..."
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Launcher | Select-Object -Last 8 | ForEach-Object { Log "  $_" }
    Log ""
    Log "Run this to verify the pipeline works end-to-end:"
    Log "  cd experiments\satcoin"
    Log "  python verify_recent_winner.py"
} elseif ($DryRun) {
    Log "DryRun -- would launch bitcoind here"
} else {
    Log "SkipStart set -- bitcoind not launched"
}

Log "=== teleport-fetch done ==="
