# teleport-all-stage.ps1
#
# ONE command on the SOURCE PC (Triton) that packages EVERYTHING for the
# destination PC:
#   1. git push  ->  source code is up-to-date on both remotes
#   2. claude-sync.ps1  ->  this Claude Code conversation is pushed to Drive
#   3. teleport-stage.ps1  ->  wallet + bitcoin.conf + built binaries + pruned
#                              datadir (~10 GB) uploaded to Drive
#
# The matching receive script is teleport-all-fetch.ps1 on Poseidon.
#
# Flags:
#   -DryRun     Preview all three phases without touching anything.
#   -SkipGit    Skip the git-push step (e.g., you already pushed).
#   -SkipConvo  Skip the claude-sync step (e.g., you don't need the chat).
#   -SkipData   Skip the bitcoin-data teleport (fastest — code + convo only).

[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$SkipGit,
    [switch]$SkipConvo,
    [switch]$SkipData
)

$ErrorActionPreference = "Continue"
$RepoRoot          = Resolve-Path (Join-Path $PSScriptRoot "..\..\..")
$ClaudeSyncScript  = "C:\Users\dizzyvinci\claude-sync\claude-sync.ps1"
$TeleportStage     = Join-Path $PSScriptRoot "teleport-stage.ps1"

function Log { param([string]$m) "[$(Get-Date -Format 'HH:mm:ss')] $m" }

Log "=== teleport-all-stage start ==="
Log "  repo:      $RepoRoot"
Log "  dry-run:   $DryRun"
Log "  skip:      Git=$SkipGit  Convo=$SkipConvo  Data=$SkipData"

# --- Phase 1: git push -------------------------------------------------------
if ($SkipGit) {
    Log "[phase 1/3] SkipGit set -- skipping code push"
} else {
    Log "[phase 1/3] git push (source code -> both remotes)"
    Push-Location $RepoRoot
    try {
        if ($DryRun) {
            Log "  DryRun -- would run: git status; git push"
            & git status --short 2>&1 | ForEach-Object { Log "    $_" }
        } else {
            & git status --short 2>&1 | ForEach-Object { Log "    $_" }
            & git push 2>&1 | ForEach-Object { Log "    $_" }
        }
    } finally { Pop-Location }
}

# --- Phase 2: claude-sync ----------------------------------------------------
if ($SkipConvo) {
    Log "[phase 2/3] SkipConvo set -- skipping conversation push"
} elseif (-not (Test-Path $ClaudeSyncScript)) {
    Log "[phase 2/3] claude-sync.ps1 not found at $ClaudeSyncScript -- skipping"
} else {
    Log "[phase 2/3] claude-sync (Claude conversation -> Drive)"
    if ($DryRun) {
        Log "  DryRun -- would run: $ClaudeSyncScript"
    } else {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $ClaudeSyncScript 2>&1 |
            ForEach-Object { Log "    $_" }
    }
}

# --- Phase 3: teleport-stage (bitcoin data) ----------------------------------
if ($SkipData) {
    Log "[phase 3/3] SkipData set -- skipping bitcoin datadir teleport"
} elseif (-not (Test-Path $TeleportStage)) {
    Log "[phase 3/3] FATAL: teleport-stage.ps1 not found at $TeleportStage"
    exit 2
} else {
    Log "[phase 3/3] teleport-stage (binaries + wallet + datadir -> Drive)"
    $stageArgs = @()
    if ($DryRun) { $stageArgs += "-DryRun" }
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $TeleportStage @stageArgs 2>&1 |
        ForEach-Object { Log "    $_" }
}

Log "=== teleport-all-stage done ==="
Log ""
Log "On the DESTINATION PC (Poseidon), run:"
Log "  git clone git@github-dizzyvinci:dizzyvinci/cgen_fetchredeem_optimization.git \$env:USERPROFILE\cgen_fetchredeem_optimization"
Log "  cd \$env:USERPROFILE\cgen_fetchredeem_optimization"
Log "  git checkout satcoin-experiment"
Log "  powershell -ExecutionPolicy Bypass -File experiments\satcoin\scripts\teleport-all-fetch.ps1"
