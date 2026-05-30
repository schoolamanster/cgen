# launch_bitcoind.ps1
#
# Hardened launcher for the satcoin experiment's local bitcoind node.
# Used by the Windows scheduled task "Bitcoind Satcoin Autostart" so that
# every restart (after reboot, logoff/logon, manual rerun) starts from a
# known-clean state. Avoids the post-reboot stale-auth gotcha where the
# process tree from a previous session inherited stale RPC credentials.
#
# Order of operations:
#   1. Locate any existing bitcoind processes.
#   2. Try graceful shutdown via `bitcoin-cli stop` (returns immediately
#      after the shutdown command is accepted; the process may still take
#      a few seconds to actually exit while it flushes chainstate).
#   3. If graceful stop doesn't work (auth broken, RPC unreachable, etc.),
#      fall back to Stop-Process -Force.
#   4. Wait for the port to be free.
#   5. Start a fresh bitcoind hidden.
#   6. Wait until RPC is up AND auth is working AND the wallet is loaded.
#
# Logs to %LOCALAPPDATA%\BitcoinAutostart\launch.log for post-mortem.
#
# Run-as: the user (interactive logon). No admin required.

$ErrorActionPreference = "Continue"

# --- config (tweak only if the install layout moves) ----------------------
$BitcoindExe = "C:\Program Files\Bitcoin\bitcoin-31.0\bin\bitcoind.exe"
$BitcoinCli  = "C:\Program Files\Bitcoin\daemon\bitcoin-cli.exe"
$RpcPort     = 8332
$LogDir      = Join-Path $env:LOCALAPPDATA "BitcoinAutostart"
$LogFile     = Join-Path $LogDir "launch.log"
$MaxWaitS    = 120  # how long to give bitcoind to come up after start

function Log {
    param([string]$msg)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg"
    Write-Output $line
    if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
    Add-Content -Path $LogFile -Value $line -Encoding utf8
}

function Test-RpcAuth {
    # Returns $true if bitcoind is up AND auth works.
    try {
        $r = & $BitcoinCli getblockcount 2>&1
        return ($LASTEXITCODE -eq 0 -and $r -match '^\d+$')
    } catch { return $false }
}

function Test-PortListening {
    param([int]$port = $RpcPort)
    return [bool] (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}

Log "=== launch_bitcoind start ==="
Log "executable: $BitcoindExe"

# Sanity: executable exists?
if (-not (Test-Path $BitcoindExe)) {
    Log "FATAL: bitcoind.exe not found at $BitcoindExe"
    exit 2
}

# Step 1+2: graceful stop attempt (no-op if nothing running).
$existing = Get-Process -Name "bitcoind" -ErrorAction SilentlyContinue
if ($existing) {
    Log "found existing bitcoind PIDs: $(($existing | ForEach-Object Id) -join ', ')"
    Log "attempting graceful stop via bitcoin-cli"
    try {
        & $BitcoinCli stop 2>&1 | ForEach-Object { Log "  cli stop: $_" }
    } catch {
        Log "  cli stop threw: $_"
    }
    # Give it up to 20s to flush + exit.
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Seconds 1
        if (-not (Get-Process -Name "bitcoind" -ErrorAction SilentlyContinue)) {
            Log "graceful stop succeeded after ${i}s"
            break
        }
    }
}

# Step 3: if anything is still alive, kill it.
$still = Get-Process -Name "bitcoind" -ErrorAction SilentlyContinue
if ($still) {
    Log "graceful stop did not complete; force-killing PIDs $(($still | ForEach-Object Id) -join ', ')"
    $still | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}

# Step 4: wait for RPC port to be free.
for ($i = 0; $i -lt 10; $i++) {
    if (-not (Test-PortListening)) {
        Log "RPC port $RpcPort is free"
        break
    }
    Log "  port $RpcPort still bound, waiting"
    Start-Sleep -Seconds 1
}

# Step 5+6: fresh start + wait for RPC+auth, with one retry if the first
# bitcoind comes up with stale RPC auth (occasionally seen when bitcoind is
# launched from inside Task Scheduler — root cause not fully isolated, but
# a force-kill + fresh start always recovers).
$MAX_ATTEMPTS = 2
$ready = $false
for ($attempt = 1; $attempt -le $MAX_ATTEMPTS; $attempt++) {
    if ($attempt -gt 1) {
        Log "auth did not come up on attempt $($attempt - 1); force-killing and retrying"
        Get-Process -Name "bitcoind" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 3
        for ($i = 0; $i -lt 10; $i++) {
            if (-not (Test-PortListening)) { break }
            Start-Sleep -Seconds 1
        }
    }
    Log "starting fresh bitcoind hidden (attempt $attempt / $MAX_ATTEMPTS)"
    $p = Start-Process -FilePath $BitcoindExe -WindowStyle Hidden -PassThru
    Log "  started PID $($p.Id) at $(Get-Date -Format 'HH:mm:ss')"
    for ($i = 0; $i -lt $MaxWaitS; $i++) {
        if (Test-RpcAuth) {
            Log "  RPC up + auth OK after ${i}s on attempt $attempt"
            $ready = $true
            break
        }
        Start-Sleep -Seconds 1
    }
    if ($ready) { break }
    Log "  WARNING: RPC/auth not available within ${MaxWaitS}s on attempt $attempt"
}
if (-not $ready) {
    Log "FATAL: all $MAX_ATTEMPTS attempts failed; bitcoind may be running but RPC auth is broken"
    Log "       run scripts\launch_bitcoind.ps1 manually to retry, or investigate %APPDATA%\Bitcoin\debug.log"
}

# Bonus: confirm wallet loaded (auto-load is set via wallet=satcoin in bitcoin.conf).
try {
    $wallets = & $BitcoinCli listwallets 2>&1
    Log "wallets loaded: $wallets"
} catch {
    Log "could not query wallets yet (still loading)"
}

Log "=== launch_bitcoind done ==="
exit 0
