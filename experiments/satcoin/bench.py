"""Time the optimized phases: fetch, conversion-to-SAT, and redemption.

This benchmark measures the parts we control. Solver time is intentionally
excluded — it's the academically-intractable part, not what we're tuning.

For each block under test, the run reports wall-clock for:
  1. fetch         — HTTP call + local hash verification (fetch_block.py)
  2. conversion    — cgen ×2 + parse + splice + target pin (build_satcoin_cnf.py)
  3. redemption    — parse solver output + hashlib verification (submit_block.py)

Run from experiments/satcoin/:
    python bench.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
OUT.mkdir(exist_ok=True)


def time_block(label: str):
    class Timer:
        def __enter__(self):
            self.t0 = time.perf_counter()
            return self
        def __exit__(self, *a):
            self.dt = time.perf_counter() - self.t0
            print(f"  {label}: {self.dt*1000:8.1f} ms")
    return Timer()


def run(*args, capture=True):
    r = subprocess.run([sys.executable, *args] if args[0].endswith(".py") else list(args),
                       capture_output=capture, text=True, cwd=HERE)
    if r.returncode != 0:
        print(r.stdout); print(r.stderr)
        sys.exit(f"command failed: {args}")
    return r


def benchmark(label: str, fetch_args: list[str], difficulty_bits: int = 8):
    print(f"\n=== {label} ===")
    header_path = OUT / f"header_{label}.json"
    cnf_path = OUT / f"satcoin_{label}.cnf"
    solver_log = OUT / f"solver_{label}.log"

    with time_block("fetch              ") as t:
        r = run("fetch_block.py", *fetch_args)
        header_path.write_text(r.stdout)
    header = json.loads(header_path.read_text())
    print(f"     -> height={header['block_height']} hash={header['block_hash'][:24]}...")

    with time_block("conversion (cgen x2 + splice + pin)") as t:
        run("build_satcoin_cnf.py",
            "--header", str(header_path),
            "--difficulty-bits", str(difficulty_bits),
            "--output", str(cnf_path))
    n_vars, n_clauses = None, None
    with open(cnf_path) as f:
        for line in f:
            if line.startswith("p cnf"):
                _, _, n_vars, n_clauses = line.split()
                break
    print(f"     -> {n_vars} vars, {n_clauses} clauses @ difficulty={difficulty_bits}")

    # For the redemption timing we need *some* solver output. We solve only
    # for difficulty=4 (always quick); for higher difficulty we just feed
    # back the same solver result, which exercises the parser identically.
    if difficulty_bits <= 4 or label == "genesis":
        # Solve once, low difficulty, just to have a real `v ...` line set.
        low_cnf = OUT / f"satcoin_{label}_low.cnf"
        run("build_satcoin_cnf.py",
            "--header", str(header_path),
            "--difficulty-bits", "4",
            "--output", str(low_cnf))
        subprocess.run([
            str(HERE.parent.parent / "tools" / "cryptominisat" / "cryptominisat5.exe"),
            "--verb", "0", str(low_cnf),
        ], stdout=open(solver_log, "w"), stderr=subprocess.DEVNULL, cwd=HERE)

    if solver_log.exists():
        with time_block("redemption (parse + verify)        ") as t:
            run("submit_block.py",
                "--header", str(header_path),
                "--solver-output", str(solver_log),
                "--difficulty-bits", "4")
    else:
        print("  redemption         : skipped (no solver output for this label)")


if __name__ == "__main__":
    benchmark("genesis", ["--height", "0"])
    benchmark("recent_easy", ["--height", "100000"])
    benchmark("current_tip", ["--tip"])
