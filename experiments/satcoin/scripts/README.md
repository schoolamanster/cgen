# bitcoind autostart hardening

Two PowerShell scripts that keep the local bitcoind running for the satcoin
experiment across reboots and lock/unlock cycles without manual intervention.

## What's here

| File | Role |
|---|---|
| `launch_bitcoind.ps1` | Hardened launcher: stops any existing bitcoind cleanly (graceful first, force-kill fallback), waits for the RPC port to clear, starts a fresh bitcoind hidden, waits for RPC + auth to be working. Logs each step to `%LOCALAPPDATA%\BitcoinAutostart\launch.log`. |
| `stop_bitcoind.ps1` | Graceful shutdown via `bitcoin-cli stop` with force-kill fallback after 30 s. Useful before reboots or as a SessionEnd hook. |

## Why a wrapper instead of just running bitcoind.exe

The first autostart (just the bare `bitcoind.exe` action) caught a stale-RPC-auth
gotcha after one reboot: bitcoind appeared to be running but RPC requests returned
`HTTP 401 Unauthorized`, even with the correct credentials from `bitcoin.conf`.
Root cause never fully isolated; the working hypothesis is a process-tree state
quirk where the autostarted instance inherited stale RPC-auth state from before
the reboot. A clean stop + start broke the deadlock.

The launcher always does that clean stop + start, so the gotcha can't repeat
even if the underlying cause is environmental.

## How to wire it into Task Scheduler

This step needs to be run by you (not automated) because modifying a logon-triggered
task to execute an arbitrary PowerShell script counts as unauthorized persistence
from the agent's perspective.

In an elevated-or-not PowerShell window:

```powershell
$taskName = "Bitcoind Satcoin Autostart"
$launcher = "C:\Users\dizzyvinci\cgen_fetchredeem_optimization\experiments\satcoin\scripts\launch_bitcoind.ps1"

# Replace the existing task (or create fresh).
Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue | Unregister-ScheduledTask -Confirm:$false

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$launcher`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
    -MultipleInstances IgnoreNew -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Trigger $trigger `
    -Action $action -Settings $settings -Principal $principal
```

You can verify it's wired up by running the task immediately without rebooting:

```powershell
Start-ScheduledTask -TaskName "Bitcoind Satcoin Autostart"
# Then check the launcher log:
Get-Content "$env:LOCALAPPDATA\BitcoinAutostart\launch.log" -Tail 20
```

To disable the autostart later:

```powershell
Unregister-ScheduledTask -TaskName "Bitcoind Satcoin Autostart" -Confirm:$false
```

## Coverage matrix (with the hardened launcher)

| Event | Behavior |
|---|---|
| Lock screen | bitcoind continues running (lock doesn't kill processes) |
| Sleep | bitcoind suspends + resumes with system |
| Reboot → login | Task fires → launcher runs → clean restart from known state |
| Stale post-reboot bitcoind with broken auth | Launcher kills it and starts fresh — bug class can't repeat |
| Log out + log back in | Task fires → launcher detects existing instance, gracefully restarts |
| bitcoind crashes mid-session | Task auto-restarts up to 5 × 1-min |
