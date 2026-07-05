# Bootstrap the satcoin experiment on a fresh PC

Step-by-step from "fresh Windows machine" to running the live-mining pipeline + a passing verification on a real Bitcoin block. Time to first PASS: **~30 minutes of active work + 1-3 days of background bitcoind sync**.

**Faster alternative if you already have another PC running the pipeline:** skip the bitcoind sync — see [scripts/POSEIDON-RESUME.md](scripts/POSEIDON-RESUME.md). Copy the wallet + pruned datadir over rclone Drive in ~30 min instead of syncing from scratch.

Linux is similar — paths and the launcher script are Windows-specific, but `python verify_recent_winner.py` itself is platform-portable.

## What you'll have at the end

- A pruned Bitcoin Core node (~10 GB) syncing your wallet
- `cgen` built natively for fast SHA-256 → CNF encoding
- CryptoMiniSat solver binary
- All Python pipeline scripts ready to run
- `verify_recent_winner.py` returning **PASS** on the current chain tip — same proof we've run 26+ times

## 1 — Clone the repo

Two GitHub remotes; either works. Pick whichever you have access to:

```powershell
# Option A: public (no auth needed)
git clone https://github.com/schoolamanster/cgen_fetchredeem_optimization.git
cd cgen_fetchredeem_optimization

# Option B: private mirror (needs SSH set up to dizzyvinci's GitHub)
git clone git@github-dizzyvinci:dizzyvinci/cgen_fetchredeem_optimization.git
cd cgen_fetchredeem_optimization

git checkout satcoin-experiment        # the work branch
```

## 2 — Install the C++ toolchain and build cgen

`cgen` is a C++ tool that encodes SHA-256 (and other ciphers) into CNF. We use it inside `build_satcoin_cnf.py`. It's not in the repo as a binary; you build it.

**Toolchain (any of these works):**
- WinLibs g++ 14+ (recommended — used to build the committed `cgeno.exe`): https://winlibs.com
- MSVC via Visual Studio 2022 (free Community edition)
- MinGW-w64 from Chocolatey: `choco install mingw`

**Build (from the repo root):**

```powershell
mingw32-make cgen_optimized       # produces cgeno.exe in the repo root
# OR if you have CMake + CLion: open the folder as a CMake project, build target 'cgeno'
```

Sanity check:
```powershell
.\cgeno.exe --version
```
Should print a version line. If it errors with "missing DLL," your g++ runtime libraries aren't on PATH — add `<winlibs-dir>\bin` to PATH or copy the needed DLLs next to `cgeno.exe`.

## 3 — Drop in CryptoMiniSat

A pre-built Windows binary (small, ~3 MB). Not bundled — gitignored under `tools/cryptominisat/cryptominisat5.exe`.

Download from the official release page:
https://github.com/msoos/cryptominisat/releases (pick the latest `cryptominisat-<ver>-win64.zip`)

Extract `cryptominisat5.exe` into `tools/cryptominisat/` (create that folder if needed):

```
cgen_fetchredeem_optimization\
  tools\
    cryptominisat\
      cryptominisat5.exe      <-- here
```

Sanity check:
```powershell
.\tools\cryptominisat\cryptominisat5.exe --version
```

## 4 — Install Bitcoin Core 31.0 (the node)

The `winget install BitcoinCoreProject.BitcoinCore` package historically ships *without* `bitcoind.exe` (only the GUI wrapper + `bitcoin-cli.exe`). Grab the official ZIP so you also have the daemon:

1. Download `bitcoin-31.0-win64.zip` from https://bitcoincore.org/en/download/ (or the v31 GitHub release)
2. Extract it. Inside the zip, copy the `bitcoin-31.0/` folder into `C:\Program Files\Bitcoin\` so that you end up with:
   ```
   C:\Program Files\Bitcoin\bitcoin-31.0\bin\bitcoind.exe
   C:\Program Files\Bitcoin\bitcoin-31.0\bin\bitcoin-cli.exe
   ```
3. (Optional but useful: keep `C:\Program Files\Bitcoin\daemon\bitcoin-cli.exe` from the winget install too — both work; some scripts reference the daemon\ path.)

## 5 — Configure bitcoin.conf

Bitcoin Core reads `%APPDATA%\Bitcoin\bitcoin.conf` (= `C:\Users\<you>\AppData\Roaming\Bitcoin\bitcoin.conf`).

Create the file (if `Roaming\Bitcoin` doesn't exist yet, create the folder first):

```ini
# Pruned node, ~10 GB disk
server=1
prune=10000
disablewallet=0
txindex=0

# Localhost-only RPC. REPLACE rpcpassword with a long random string of YOUR choosing.
rpcbind=127.0.0.1
rpcallowip=127.0.0.1
rpcuser=satcoin
rpcpassword=GENERATE_A_40_CHAR_RANDOM_STRING_HERE

# Faster sync
dbcache=2048

# Auto-load this wallet on bitcoind start
wallet=satcoin
```

Generate a strong rpcpassword in PowerShell:
```powershell
[Convert]::ToBase64String([byte[]](1..30 | ForEach-Object { Get-Random -Min 0 -Max 255 }))
```
Paste the output as `rpcpassword=`.

## 6 — Start bitcoind and create the wallet

Two ways to start bitcoind:

### Recommended: the launcher script (auto-restart, log, future-proof)

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File `
  ".\experiments\satcoin\scripts\launch_bitcoind.ps1"
```

This pins `-datadir` (don't skip — without it bitcoind on some Windows session contexts decides to use a *different* default datadir at `%LOCALAPPDATA%\Bitcoin` and silently grows a separate 200+ GB full sync; see `scripts/README.md` for the full diagnosis).

The launcher logs to `%LOCALAPPDATA%\BitcoinAutostart\launch.log`.

### Manual (if you want to see output directly)

```powershell
& "C:\Program Files\Bitcoin\bitcoin-31.0\bin\bitcoind.exe" `
  -datadir="C:\Users\$env:USERNAME\AppData\Roaming\Bitcoin"
```

### Create the wallet (first time only)

After bitcoind is up:

```powershell
& "C:\Program Files\Bitcoin\daemon\bitcoin-cli.exe" `
  -datadir="C:\Users\$env:USERNAME\AppData\Roaming\Bitcoin" `
  createwallet "satcoin"

& "C:\Program Files\Bitcoin\daemon\bitcoin-cli.exe" `
  -datadir="C:\Users\$env:USERNAME\AppData\Roaming\Bitcoin" `
  -rpcwallet=satcoin getnewaddress "mining_payout" "bech32"
```

The second command prints your payout address. If/when the lottery hits, that's where the coinbase reward lands.

### Wait for sync

```powershell
# Check sync status anytime:
& "C:\Program Files\Bitcoin\daemon\bitcoin-cli.exe" `
  -datadir="C:\Users\$env:USERNAME\AppData\Roaming\Bitcoin" getblockchaininfo
```

Look for `"initialblockdownload": false`. Pruned-mode full sync takes 1-3 days depending on bandwidth and CPU. You can leave bitcoind running in the background.

## 7 — Configure logon autostart (optional but recommended)

Drop a shortcut in the user Startup folder so bitcoind comes up on every logon:

```powershell
$launcher = "$(Get-Location)\experiments\satcoin\scripts\launch_bitcoind.ps1"
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

Don't use Task Scheduler — observed to spawn bitcoind in a context that misreads `%APPDATA%` (see `scripts/README.md` for the full incident).

## 8 — Verify the pipeline works

You need Python 3.10+ on PATH. No `pip install` needed; the pipeline only uses stdlib.

```powershell
cd experiments\satcoin
python verify_recent_winner.py
```

Expected output (numbers will reflect the current chain tip):

```
[1/6] Most recent block: height 954,681  hash 00000000...
[2/6] Header: 80 bytes, displayed nonce=441,156,613, target=0x0000000000000000...
[3/6] CNF built: 87,971 vars, 455,900 clauses in 4162 ms
[4/6] Pinned 32 nonce unit clauses (network-order bytes 05844b1a)
      first 8 units: -1 -2 -3 -4 -5 +6 -7 +8
[5/6] Solver: s SATISFIABLE in 14952 ms
[6/6] Recovered nonce from solver assignment: 441,156,613
      Expected (chain header):                 441,156,613
      Match: True

PASS: pipeline correctly verifies block 954,681's real winning nonce.
```

**That's the end-to-end proof.** It fetches the actual current Bitcoin tip, builds a CNF from its real header, pins the winning nonce as 32 unit clauses, runs CryptoMiniSat, confirms SAT, and round-trips the nonce out of the satisfying assignment — bit-perfect with what's on the chain.

If you want the full 88,000-variable SAT solution dumped to a file (for paranoid-level verification later):
```powershell
python verify_recent_winner.py --save-assignment auto
```
Produces `out/recent_winner/assignment_<height>_<hashpfx>.txt` with every variable's value plus a SHA-256 fingerprint.

## 9 — Run the offline test suites (no node needed)

These exercise the CNF construction and encoding logic against historical test vectors. All run in seconds:

```powershell
python test_nonce_pinning.py           # 8 tests — byte-order helper
python test_block_construction.py      # 9 tests — varint, BIP34, segwit, merkle
python tests.py                        # 9 tests — SAT encoding vs real blocks
```

These plus the live `test_historical_reconstruction.py` and `test_historical_submit.py` (which use your local node) give 42+ passing tests covering every byte-level dimension of the pipeline.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `RuntimeError: bitcoind unreachable` | bitcoind isn't running | Re-run `launch_bitcoind.ps1` |
| `Authorization failed: Incorrect rpcuser or rpcpassword` | bitcoind started without our `-datadir`, used a different default and bypassed our conf | Stop everything and re-run `launch_bitcoind.ps1` — it pins datadir explicitly |
| `cli stop: error: Authorization failed` followed by force-kill in launch.log | Same as above — launcher recovers automatically | No action needed |
| Disk space drops by hundreds of GB unexpectedly | The `Local\Bitcoin` orphan growing because something else spawned bitcoind without `-datadir` | Check `Get-ChildItem C:\Users\<you>\AppData\Local\Bitcoin` — if it exists with multi-GB blocks/, delete it (`Remove-Item -Recurse -Force`). Investigate the secondary launcher. |
| `cgen` build errors with template/concept errors | Old g++; need g++ ≥ 13 | Install WinLibs g++ 14+ |
| `cryptominisat5.exe` not found | Step 3 skipped | Download + drop into `tools/cryptominisat/` |
| Verify says `meets_real_bitcoin_target: false` for the chain tip | The encoding is wrong (real bug) | File a github issue — this should never happen on the committed code |

## Repo structure (for orientation)

```
cgen_fetchredeem_optimization/
├── cgen.cpp etc.                  # vsklad/cgen upstream + our additions
├── CMakeLists.txt, makefile       # build cgeno.exe
├── tools/cryptominisat/           # solver binary you drop in
├── experiments/satcoin/
│   ├── BOOTSTRAP.md               # this file
│   ├── README.md                  # the academic framing + math
│   ├── nonce_pinning.py           # single-source-of-truth for nonce↔CNF
│   ├── block_template.py          # full block construction
│   ├── build_satcoin_cnf.py       # cgen wrapper + target cascade
│   ├── submit_block.py            # verify+broadcast path
│   ├── fetch_block.py             # API/RPC header fetcher
│   ├── verify_recent_winner.py    # what you run to prove the pipeline works
│   ├── mine_loop.py               # continuous mining daemon
│   ├── live_bench.py              # end-to-end timing benchmark
│   ├── wallet.py                  # read-only wallet inspector
│   ├── test_*.py                  # 5 test suites, 42+ tests
│   └── scripts/
│       ├── launch_bitcoind.ps1    # the autostart launcher
│       ├── stop_bitcoind.ps1
│       └── README.md              # autostart + the diagnostic story
```

## What's NOT in this repo (intentional)

- `cgeno.exe` — built artifact (step 2)
- `tools/cryptominisat/cryptominisat5.exe` — solver binary (step 3)
- Bitcoin Core (step 4) — installed system-wide
- `%APPDATA%\Bitcoin\` — your local node's data (step 5+6)
- `out/` directories — runtime artifacts (regenerated by each run)

That's it. After step 8 returns PASS, you have the same infrastructure that's been running 26+ consecutive verifications across a month on the original machine.
