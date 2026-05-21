"""Continuous mining daemon for the satcoin experiment.

Wraps the single-shot pipeline (block_template -> build_satcoin_cnf -> solver ->
submit_block) in a loop that:

  - Detects chain-tip changes (polling getbestblockhash every poll_interval_s)
  - On every new tip: drops in-flight work, fetches fresh template, restarts
  - Handles either solver outcome correctly:
      * "won" — submit, log, continue from height+1
      * "stale" / didn't win — keep listening for the next block
  - Survives transient RPC errors (bitcoind blip, network drop) with backoff
  - Refreshes template if the same tip persists past `stale_after_s` (mempool
    churns; updated coinbase fees matter for a real miner)

Two modes for academic experimentation:

  --simulate-win
      On each new tip, generate a synthetic SAT-solver log using the
      template's placeholder nonce, run it through the redemption pipeline.
      Under real difficulty this will say "doesn't meet target," but the
      code path is exactly what a real win would exercise. Useful to time
      the win-handling subroutine and verify it doesn't break.

  --auto-submit
      Actually call `submitblock` via RPC. For placeholder nonces this
      returns 'high-hash' (rejected for not meeting target) — which proves
      the submit path runs end-to-end. Real wins would return 'accepted'.

The loop runs forever by default; --max-iterations N halts after N tip-poll
cycles for testing.

Usage:
    python mine_loop.py                           # listen-only, no win sim
    python mine_loop.py --simulate-win            # simulate 0-second solver on every new tip
    python mine_loop.py --simulate-win --auto-submit  # also call submitblock (will be rejected)
    python mine_loop.py --max-iterations 5 --poll-interval-s 1  # short test run

State printed continuously to stdout. Ctrl+C exits cleanly.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import signal
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from block_template import rpc, get_template_and_payout, assemble_block  # noqa: E402


@dataclasses.dataclass
class LoopStats:
    iterations: int = 0
    chain_advances: int = 0
    templates_built: int = 0
    cnfs_built: int = 0
    redemption_attempts: int = 0
    submits_attempted: int = 0
    submits_accepted: int = 0
    submits_rejected: int = 0
    rpc_failures: int = 0


# A user-friendly signal handler that flips a flag instead of dying mid-write.
_STOP = False
def _on_signal(sig, frame):
    global _STOP
    _STOP = True
signal.signal(signal.SIGINT, _on_signal)
try:
    signal.signal(signal.SIGTERM, _on_signal)
except (AttributeError, ValueError):
    pass  # Windows doesn't have SIGTERM in all Python builds.


def now_ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def log(msg: str) -> None:
    print(f"[{now_ts()}] {msg}", flush=True)


def safe_rpc(method: str, params=None, wallet=None, retries: int = 3, backoff_s: float = 1.0):
    """Retry RPC on transient errors. Returns None on persistent failure."""
    for attempt in range(retries):
        try:
            return rpc(method, params or [], wallet=wallet)
        except RuntimeError as e:
            if attempt == retries - 1:
                log(f"RPC {method} failed after {retries} retries: {e}")
                return None
            time.sleep(backoff_s * (attempt + 1))
    return None


# ---------------------------------------------------------------------------
# Pipeline pieces (use the existing scripts via subprocess for parity with
# how the user would invoke them on the command line)
# ---------------------------------------------------------------------------

def build_template_and_cnf(work_dir: Path) -> dict | None:
    """Fetch a live template and build the CNF for it. Returns the parsed
    header_json on success, None on failure."""
    header_path = work_dir / "header.json"
    cnf_path = work_dir / "satcoin.cnf"
    # We could call the script via subprocess, but importing the function
    # directly skips a Python startup and keeps everything in one process —
    # the optimization the previous review identified as highest-ROI.
    try:
        template, payout_script_hex = get_template_and_payout()
    except RuntimeError as e:
        log(f"getblocktemplate failed: {e}")
        return None
    header_json = assemble_block(template, payout_script_hex)
    header_path.write_text(json.dumps(header_json))
    # Still call build_satcoin_cnf.py via subprocess — cgen needs its own
    # subprocess anyway. (A future optimization could in-line this too.)
    res = subprocess.run(
        [sys.executable, str(HERE / "build_satcoin_cnf.py"),
         "--header", str(header_path),
         "--output", str(cnf_path)],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        log(f"build_satcoin_cnf.py failed: {res.stderr[:200]}")
        return None
    header_json["_cnf_path"] = str(cnf_path)
    return header_json


def synthesize_solver_log(header_json: dict, work_dir: Path) -> Path:
    """Generate a synthetic 'v ...' line using the template's placeholder
    nonce. Stands in for a real solver under the 0-second premise."""
    nonce_bytes = bytes.fromhex(header_json["raw_header_hex"][152:160])
    units = []
    for byte_i, b in enumerate(nonce_bytes):
        for bit in range(7, -1, -1):
            var = byte_i * 8 + (7 - bit) + 1
            units.append((1 if (b >> bit) & 1 else -1) * var)
    # We have to fill out the rest of the variable assignment for submit_block.py.
    n_vars = None
    with open(header_json["_cnf_path"]) as f:
        for line in f:
            if line.startswith("p cnf"):
                n_vars = int(line.split()[2])
                break
    rest = " ".join(str(v) for v in range(33, n_vars + 1))
    log_path = work_dir / "solver.log"
    log_path.write_text(
        f"s SATISFIABLE\nv {' '.join(str(u) for u in units)} {rest} 0\n")
    return log_path


def run_redemption(header_json: dict, solver_log: Path, work_dir: Path,
                  auto_submit: bool) -> dict:
    """Run submit_block.py. Returns a dict with parsed verdict info."""
    header_path = work_dir / "header_for_redemption.json"
    header_path.write_text(json.dumps(header_json))
    args = [sys.executable, str(HERE / "submit_block.py"),
            "--header", str(header_path),
            "--solver-output", str(solver_log)]
    if auto_submit:
        args.append("--submit")
    res = subprocess.run(args, capture_output=True, text=True)
    out = res.stdout + "\n" + res.stderr
    result = {
        "returncode": res.returncode,
        "stdout": res.stdout,
        "stderr": res.stderr,
        "submit_status": None,
        "meets_real_bitcoin_target": None,
    }
    # Parse the JSON the verify step prints.
    try:
        start = res.stdout.index("{"); end = res.stdout.rindex("}") + 1
        payload = json.loads(res.stdout[start:end])
        result["meets_real_bitcoin_target"] = payload.get("meets_real_bitcoin_target")
    except (ValueError, json.JSONDecodeError):
        pass
    # The submit line, if present, looks like: "[submit] STATUS: detail"
    for line in out.splitlines():
        if line.startswith("[submit]"):
            parts = line.split(":", 2)
            if len(parts) >= 2:
                status_token = parts[0].replace("[submit]", "").strip()
                result["submit_status"] = status_token or (parts[1].strip().split()[0] if len(parts) > 1 else None)
                break
    return result


# ---------------------------------------------------------------------------
# The loop itself
# ---------------------------------------------------------------------------

def run_loop(*,
             poll_interval_s: float = 2.0,
             max_iterations: int | None = None,
             stale_after_s: float = 60.0,
             simulate_win: bool = False,
             auto_submit: bool = False,
             work_dir: Path | None = None) -> LoopStats:
    """Main mining loop. Returns final stats."""
    if work_dir is None:
        work_dir = HERE / "out" / "loop"
    work_dir.mkdir(parents=True, exist_ok=True)
    stats = LoopStats()

    current_tip: str | None = None
    current_header_json: dict | None = None
    template_age_t0: float = 0.0

    log(f"=== mine_loop starting (poll={poll_interval_s}s, "
        f"simulate_win={simulate_win}, auto_submit={auto_submit}) ===")

    while not _STOP:
        if max_iterations is not None and stats.iterations >= max_iterations:
            log(f"max_iterations reached ({max_iterations}); exiting")
            break
        stats.iterations += 1
        iter_t0 = time.time()

        # 1. Poll chain tip.
        tip = safe_rpc("getbestblockhash")
        if tip is None:
            stats.rpc_failures += 1
            log(f"iter#{stats.iterations}: RPC unreachable, sleeping {poll_interval_s}s")
            time.sleep(poll_interval_s)
            continue

        # 2. Decide whether to (re)build template.
        need_refresh = (current_tip is None) or (tip != current_tip) \
                       or (time.time() - template_age_t0 > stale_after_s)
        if tip != current_tip:
            if current_tip is not None:
                stats.chain_advances += 1
                log(f"iter#{stats.iterations}: CHAIN ADVANCED — tip is now {tip[:16]}..., "
                    f"dropping any in-flight work")
            else:
                log(f"iter#{stats.iterations}: first tip observed: {tip[:16]}...")
            current_tip = tip

        if need_refresh:
            log(f"iter#{stats.iterations}: building template + CNF for next block on top of "
                f"{current_tip[:16]}...")
            header_json = build_template_and_cnf(work_dir)
            if header_json is None:
                log(f"iter#{stats.iterations}: template/CNF build failed; will retry next iteration")
                time.sleep(poll_interval_s)
                continue
            current_header_json = header_json
            template_age_t0 = time.time()
            stats.templates_built += 1
            stats.cnfs_built += 1
            log(f"iter#{stats.iterations}: built CNF for height "
                f"{current_header_json['block_height']} "
                f"({current_header_json['extras']['tx_count_total']} txns, "
                f"{current_header_json['coinbase_value_btc']:.6f} BTC reward, "
                f"payout-> {current_header_json['payout_script_hex'][:16]}...)")

        # 3. If --simulate-win, pretend the 0-second solver returned a result.
        if simulate_win and current_header_json is not None:
            # Re-verify the tip hasn't moved since we built (catches the race
            # where a block landed during build). If it did, drop this attempt.
            tip_check = safe_rpc("getbestblockhash")
            if tip_check != current_tip:
                log(f"iter#{stats.iterations}: stale race — tip moved during build "
                    f"({current_tip[:16]}... -> {tip_check[:16]}...). Dropping attempt.")
                current_tip = tip_check
                current_header_json = None
                stats.chain_advances += 1
                continue
            solver_log = synthesize_solver_log(current_header_json, work_dir)
            res = run_redemption(current_header_json, solver_log, work_dir, auto_submit)
            stats.redemption_attempts += 1
            if auto_submit:
                stats.submits_attempted += 1
                st = res.get("submit_status") or "?"
                if st == "accepted":
                    stats.submits_accepted += 1
                    log(f"iter#{stats.iterations}: !!! BLOCK ACCEPTED !!! "
                        f"height {current_header_json['block_height']} "
                        f"reward routed to wallet")
                    # After winning, the chain has advanced; force refresh on next loop.
                    current_tip = None
                    current_header_json = None
                else:
                    stats.submits_rejected += 1
                    log(f"iter#{stats.iterations}: submit verdict: {st} "
                        f"(expected for placeholder nonce on mainnet target)")
            else:
                meets = res.get("meets_real_bitcoin_target")
                log(f"iter#{stats.iterations}: redemption rehearsal done, "
                    f"meets_real_target={meets} (placeholder nonce; True is only possible "
                    f"with a real solution)")

        # 4. Sleep before next iteration.
        elapsed = time.time() - iter_t0
        sleep_for = max(0.0, poll_interval_s - elapsed)
        if sleep_for > 0:
            time.sleep(sleep_for)

    log(f"=== mine_loop stopped ===")
    log(f"final stats: {dataclasses.asdict(stats)}")
    return stats


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--poll-interval-s", type=float, default=2.0,
                   help="how often to check chain tip (default 2.0)")
    p.add_argument("--max-iterations", type=int, default=None,
                   help="halt after N iterations (default: forever)")
    p.add_argument("--stale-after-s", type=float, default=60.0,
                   help="rebuild template if same tip persists this long (default 60)")
    p.add_argument("--simulate-win", action="store_true",
                   help="on every new tip, run redemption rehearsal with placeholder nonce")
    p.add_argument("--auto-submit", action="store_true",
                   help="actually call submitblock (will be rejected for placeholder; "
                        "use with --simulate-win)")
    args = p.parse_args()
    run_loop(
        poll_interval_s=args.poll_interval_s,
        max_iterations=args.max_iterations,
        stale_after_s=args.stale_after_s,
        simulate_win=args.simulate_win,
        auto_submit=args.auto_submit,
    )


if __name__ == "__main__":
    main()
