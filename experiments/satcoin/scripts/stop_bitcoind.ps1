# stop_bitcoind.ps1
#
# Graceful shutdown of bitcoind via bitcoin-cli stop, with force-kill fallback.
# Used by the SessionEnd hook so bitcoind flushes its chainstate cleanly on
# Claude session exit (catches the case where Windows is going to reboot
# right after, avoiding the stale-state class of bug).

$ErrorActionPreference = "Continue"
$BitcoinCli = "C:\Program Files\Bitcoin\daemon\bitcoin-cli.exe"
$LogDir     = Join-Path $env:LOCALAPPDATA "BitcoinAutostart"
$LogFile    = Join-Path $LogDir "launch.log"

function Log {
    param([string]$msg)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg"
    Write-Output $line
    if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
    Add-Content -Path $LogFile -Value $line -Encoding utf8
}

$existing = Get-Process -Name "bitcoind" -ErrorAction SilentlyContinue
if (-not $existing) {
    Log "stop_bitcoind: nothing to stop"
    exit 0
}
Log "stop_bitcoind: requesting graceful shutdown"
try { & $BitcoinCli stop 2>&1 | ForEach-Object { Log "  $_" } } catch { Log "  cli stop threw: $_" }

for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    if (-not (Get-Process -Name "bitcoind" -ErrorAction SilentlyContinue)) {
        Log "stop_bitcoind: graceful exit after ${i}s"
        exit 0
    }
}
Log "stop_bitcoind: graceful exit timed out, forcing"
Get-Process -Name "bitcoind" -ErrorAction SilentlyContinue | Stop-Process -Force
exit 0
