# Resume satcoin on Poseidon (or any second PC)

Copy-paste playbook for landing the full satcoin setup on a new Windows box
after the source PC has already run `teleport-stage.ps1` (uploaded wallet +
built binaries + pruned datadir to Google Drive at
`infinityze:teleport/satcoin/`).

**Time from cold to working: ~30 minutes** — most of it the ~10 GB download.

## Prereqs on Poseidon (one-time)

Only skip these if you know they're already done.

```powershell
# 1. Bitcoin Core 31.0 installed
#    Download bitcoin-31.0-win64.zip from https://bitcoincore.org/en/download/
#    and extract so bitcoind.exe ends up at:
#      C:\Program Files\Bitcoin\bitcoin-31.0\bin\bitcoind.exe

# 2. Git + gh authed as dizzyvinci
gh auth status

# 3. SSH alias github-dizzyvinci (copy ~/.ssh/config + id_dizzyvinci from Triton
#    OR create a fresh key + add to the dizzyvinci GitHub account)
ssh -T git@github-dizzyvinci     # should say "Hi dizzyvinci!"

# 4. rclone with infinityze: remote (copy ~/.config/rclone/rclone.conf from
#    Triton, or `rclone config` fresh to auth Google Drive)
rclone lsd infinityze: --max-depth 1
```

## Step 1 — clone the repo

```powershell
git clone git@github-dizzyvinci:dizzyvinci/cgen_fetchredeem_optimization.git $env:USERPROFILE\cgen_fetchredeem_optimization
cd $env:USERPROFILE\cgen_fetchredeem_optimization
git checkout satcoin-experiment
```

## Step 2 — refresh the Drive token if needed

```powershell
rclone lsd infinityze:teleport/satcoin/     # if this errors, run the next line:
rclone config reconnect infinityze:         # opens browser for OAuth
```

## Step 3 — pull the bundle and launch bitcoind

**One command does everything:**

```powershell
powershell -ExecutionPolicy Bypass -File experiments\satcoin\scripts\teleport-fetch.ps1
```

This runs for ~20-30 min. It will:
1. Download `cgeno.exe` + `cryptominisat5.exe` into the repo (small)
2. Download `bitcoin.conf` + `satcoin/` wallet folder into `%APPDATA%\Bitcoin\`
3. Download `blocks/` + `chainstate/` into `%APPDATA%\Bitcoin\` (~10 GB)
4. Launch bitcoind via `launch_bitcoind.ps1` (which pins `-datadir` explicitly
   so the 277 GB `Local\Bitcoin` orphan bug can't recur — see
   `scripts/README.md` for the story)

`teleport-fetch.ps1` uses `$env:APPDATA` (not a hardcoded username), so it
works whether this user is `dizzyvinci`, `infin`, or anything else.

## Step 4 — verify the pipeline works

```powershell
cd experiments\satcoin
python verify_recent_winner.py
```

Expected: `PASS: pipeline correctly verifies block <height>'s real winning nonce.`
Runs in ~15-30 s once the CNF is built.

## Step 5 — CRITICAL if you're about to retire Triton — confirm wallet identity

The reason for teleport is preserving the wallet + payout address across
machines. Confirm both are byte-identical on Poseidon:

```powershell
# should print bc1qzdd22gcy0qn5fcz3z4y03f2ua757kmg7xltkck
& "C:\Program Files\Bitcoin\daemon\bitcoin-cli.exe" `
  -datadir="$env:APPDATA\Bitcoin" `
  -rpcwallet=satcoin `
  getaddressesbylabel "mining_payout"
```

If the address matches the one on Triton, spending authority transferred
correctly. Only after this returns green is it safe to decommission Triton.

## Step 6 — set up autostart on Poseidon (optional but recommended)

```powershell
$launcher = "$env:USERPROFILE\cgen_fetchredeem_optimization\experiments\satcoin\scripts\launch_bitcoind.ps1"
$startup  = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup"
$shortcut = Join-Path $startup "Bitcoind Satcoin Autostart.lnk"
$wsh = New-Object -ComObject WScript.Shell
$lnk = $wsh.CreateShortcut($shortcut)
$lnk.TargetPath       = "powershell.exe"
$lnk.Arguments        = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$launcher`""
$lnk.WorkingDirectory = (Split-Path $launcher)
$lnk.WindowStyle      = 7
$lnk.Save()
```

Now every logon on Poseidon starts bitcoind cleanly.

## Step 7 — set up claude-conversations too (so you can continue this chat)

```powershell
git clone git@github-dizzyvinci:dizzyvinci/claude-conversations.git $env:USERPROFILE\claude-conversations

# drop the raw JSONL where Claude Code expects it
$sess = "$env:USERPROFILE\claude-conversations\sessions\2026-05-18-satcoin-cnf-mining-lab\raw-jsonl\d56ea666-469d-410b-91c9-14b0c3d126b1.jsonl"
$dest = "$env:USERPROFILE\.claude\projects\C--Users-dizzyvinci"
New-Item -ItemType Directory -Path $dest -Force | Out-Null
Copy-Item $sess $dest
```

Open Claude Code on Poseidon; that session should now appear in the resume
list. Also: `claude-sync.ps1` (from the claude-sync repo) will keep this and
all future sessions bidirectionally synced.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `rclone: couldn't fetch token: invalid_grant` | `rclone config reconnect infinityze:` — opens browser for one-time OAuth |
| bitcoind not launching | Run `powershell -File experiments\satcoin\scripts\launch_bitcoind.ps1` manually and read the log at `%LOCALAPPDATA%\BitcoinAutostart\launch.log` |
| `verify_recent_winner.py` says `bitcoind unreachable` | bitcoind is off; run launcher again |
| Wallet address doesn't match Triton's | Something went wrong with the datadir/satcoin transfer. Do NOT delete Triton yet; re-run teleport-stage on Triton + teleport-fetch on Poseidon |
| Local\Bitcoin folder starts growing after launch | Shouldn't happen (launcher pins `-datadir`), but if it does: kill bitcoind, delete `%LOCALAPPDATA%\Bitcoin\`, ensure launcher is the only autostart |

## What you have when done

- Same wallet, same payout address, same private keys as Triton
- Same pruned pipeline, same 30 test/verify commands
- Continued Claude Code conversation
- Everything Poseidon-native — no residual dependence on Triton being alive
