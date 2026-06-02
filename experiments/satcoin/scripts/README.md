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

## How to wire it into autostart

**Current mechanism: Startup-folder shortcut** (not Task Scheduler).

We tried a logon-triggered scheduled task first. It fired the launcher
script — the beacon line at the top of the script confirmed it ran — but
the bitcoind it spawned consistently came up with broken RPC auth and
the launcher's regular log writes silently failed. Even the retry loop
couldn't recover; both attempts produced broken bitcoinds in that
context. The root cause is something about how bitcoind interprets its
configuration when spawned by `Start-Process` under the task scheduler's
"InteractiveToken" context — `$env:SESSIONNAME` is missing in that
process and bitcoind appears to misread the conf in a way the launcher
script can't see.

The startup-folder shortcut runs the launcher at logon in a **true**
interactive session, equivalent to the user double-clicking a shortcut
themselves. That spawn context produces a fresh-auth bitcoind every
time (verified by triggering the shortcut directly in the same session).

To install / re-install the shortcut:

```powershell
$launcher = "C:\Users\dizzyvinci\cgen_fetchredeem_optimization\experiments\satcoin\scripts\launch_bitcoind.ps1"
$startup  = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup"
$shortcut = Join-Path $startup "Bitcoind Satcoin Autostart.lnk"

$wsh = New-Object -ComObject WScript.Shell
$lnk = $wsh.CreateShortcut($shortcut)
$lnk.TargetPath       = "powershell.exe"
$lnk.Arguments        = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$launcher`""
$lnk.WorkingDirectory = (Split-Path $launcher)
$lnk.WindowStyle      = 7  # minimized
$lnk.Description      = "Launch the satcoin experiment's local bitcoind node at logon"
$lnk.Save()
```

If you previously created the scheduled task, disable it so you don't
have two autostart mechanisms fighting each other:

```powershell
Disable-ScheduledTask -TaskName "Bitcoind Satcoin Autostart" -ErrorAction SilentlyContinue
# Or fully remove:
# Unregister-ScheduledTask -TaskName "Bitcoind Satcoin Autostart" -Confirm:$false
```

You can verify the shortcut by invoking it directly (same code path as logon):

```powershell
Invoke-Item "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\Bitcoind Satcoin Autostart.lnk"
Get-Content "$env:LOCALAPPDATA\BitcoinAutostart\launch.log" -Tail 20
```

A clean shortcut-triggered run completes in ~15-20s, ending with
"`=== launch_bitcoind done ===`" and bitcoind RPC + auth working.

## Coverage matrix (with the hardened launcher)

| Event | Behavior |
|---|---|
| Lock screen | bitcoind continues running (lock doesn't kill processes) |
| Sleep | bitcoind suspends + resumes with system |
| Reboot → login | Task fires → launcher runs → clean restart from known state |
| Stale post-reboot bitcoind with broken auth | Launcher kills it and starts fresh — bug class can't repeat |
| Log out + log back in | Task fires → launcher detects existing instance, gracefully restarts |
| bitcoind crashes mid-session | Task auto-restarts up to 5 × 1-min |
