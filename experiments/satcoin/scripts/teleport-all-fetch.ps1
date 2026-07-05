# teleport-all-fetch.ps1
#
# ONE command on the DESTINATION PC (Poseidon) that receives EVERYTHING
# staged by teleport-all-stage.ps1 on Triton:
#   1. git pull  ->  code is up-to-date locally (assumes repo already cloned)
#   2. claude-sync.ps1  ->  Claude conversations pulled from Drive
#   3. teleport-fetch.ps1  ->  wallet + bitcoin.conf + binaries + pruned datadir
#                              placed in the right paths, bitcoind launched
#
# Flags:
#   -DryRun     Preview all three phases without touching anything.
#   -SkipGit    Skip the git-pull step.
#   -SkipConvo  Skip the claude-sync step.
#   -SkipData   Skip the bitcoin-data teleport.

[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$SkipGit,
    [switch]$SkipConvo,
    [switch]$SkipData
)

$ErrorActionPreference = "Continue"
$RepoRoot         = Resolve-Path (Join-Path $PSScriptRoot "..\..\..")
$ClaudeSyncScript = "$env:USERPROFILE\claude-sync\claude-sync.ps1"
$TeleportFetch    = Join-Path $PSScriptRoot "teleport-fetch.ps1"

function Log { param([string]$m) "[$(Get-Date -Format 'HH:mm:ss')] $m" }

Log "=== teleport-all-fetch start ==="
Log "  repo:      $RepoRoot"
Log "  dry-run:   $DryRun"
Log "  skip:      Git=$SkipGit  Convo=$SkipConvo  Data=$SkipData"

# --- Phase 1: git pull -------------------------------------------------------
if ($SkipGit) {
    Log "[phase 1/3] SkipGit set -- skipping code pull"
} else {
    Log "[phase 1/3] git pull (source code <- remote)"
    Push-Location $RepoRoot
    try {
        if ($DryRun) {
            Log "  DryRun -- would run: git fetch; git pull"
            & git status --short 2>&1 | ForEach-Object { Log "    $_" }
        } else {
            & git fetch 2>&1 | ForEach-Object { Log "    $_" }
            & git pull 2>&1 | ForEach-Object { Log "    $_" }
        }
    } finally { Pop-Location }
}

# --- Phase 2: claude-sync ----------------------------------------------------
if ($SkipConvo) {
    Log "[phase 2/3] SkipConvo set -- skipping conversation pull"
} elseif (-not (Test-Path $ClaudeSyncScript)) {
    Log "[phase 2/3] claude-sync.ps1 not found at $ClaudeSyncScript"
    Log "  See https://github.com/dizzyvinci/claude-sync for the setup. Skipping."
} else {
    Log "[phase 2/3] claude-sync (Claude conversations <- Drive)"
    if ($DryRun) {
        Log "  DryRun -- would run: $ClaudeSyncScript"
    } else {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $ClaudeSyncScript 2>&1 |
            ForEach-Object { Log "    $_" }
    }
}

# --- Phase 3: teleport-fetch (bitcoin data) ----------------------------------
if ($SkipData) {
    Log "[phase 3/3] SkipData set -- skipping bitcoin datadir teleport"
} elseif (-not (Test-Path $TeleportFetch)) {
    Log "[phase 3/3] FATAL: teleport-fetch.ps1 not found at $TeleportFetch"
    exit 2
} else {
    Log "[phase 3/3] teleport-fetch (binaries + wallet + datadir <- Drive)"
    $fetchArgs = @()
    if ($DryRun) { $fetchArgs += "-DryRun" }
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $TeleportFetch @fetchArgs 2>&1 |
        ForEach-Object { Log "    $_" }
}

Log "=== teleport-all-fetch done ==="
Log ""
Log "To confirm the pipeline is working here, run:"
Log "  cd experiments\satcoin"
Log "  python verify_recent_winner.py"
Log ""
Log "To open the just-transferred Claude conversation, look in:"
Log "  \$env:USERPROFILE\.claude\projects\C--Users-dizzyvinci\"
Log "  (files named <session-id>.jsonl)"
