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


def benchmark(label: str, fetch_args: list[str], synthetic_target: str | None = None):
    """Time fetch + conversion (+ redemption when we have a solver log).

    `synthetic_target` lets us produce a solvable instance for redemption
    timing — at the real Bitcoin target the solver doesn't terminate, so
    we override the target for the *solve* step only. The fetch and
    conversion timings reported are with the real network target.
    """
    print(f"\n=== {label} ===")
    header_path = OUT / f"header_{label}.json"
    cnf_path = OUT / f"satcoin_{label}.cnf"
    solver_log = OUT / f"solver_{label}.log"

    with time_block("fetch                              ") as t:
        r = run("fetch_block.py", *fetch_args)
        header_path.write_text(r.stdout)
    header = json.loads(header_path.read_text())
    print(f"     -> height={header['block_height']} hash={header['block_hash'][:24]}...")

    with time_block("conversion (cgen x2 + splice + target)") as t:
        run("build_satcoin_cnf.py",
            "--header", str(header_path),
            "--output", str(cnf_path))
    n_vars, n_clauses = None, None
    with open(cnf_path) as f:
        for line in f:
            if line.startswith("p cnf"):
                _, _, n_vars, n_clauses = line.split()
                break
    real_target = header['fields']['target']
    print(f"     -> {n_vars} vars, {n_clauses} clauses @ real target {real_target[:18]}...")

    # Redemption timing needs a real solver output to parse. The real target
    # won't terminate in any reasonable wall-time, so for THIS measurement
    # only we relax the target enough to be solvable in seconds. The
    # redemption code path is identical either way — it parses `v` lines,
    # extracts the nonce, hashlib-verifies. So the timing is representative.
    if synthetic_target is not None:
        easy_cnf = OUT / f"satcoin_{label}_easy.cnf"
        run("build_satcoin_cnf.py",
            "--header", str(header_path),
            "--target", synthetic_target,
            "--output", str(easy_cnf))
        subprocess.run([
            str(HERE.parent.parent / "tools" / "cryptominisat" / "cryptominisat5.exe"),
            "--verb", "0", str(easy_cnf),
        ], stdout=open(solver_log, "w"), stderr=subprocess.DEVNULL, cwd=HERE)

    if solver_log.exists():
        with time_block("redemption (parse + verify)           ") as t:
            run("submit_block.py",
                "--header", str(header_path),
                "--solver-output", str(solver_log),
                "--target", synthetic_target)
    else:
        print("  redemption                          : skipped (no solver output for this label)")


if __name__ == "__main__":
    # For redemption timing, give the *genesis* benchmark a relaxed synthetic
    # target (~12 zeros) so we get a real solver assignment to parse. The
    # other blocks use the real network target; their conversion is timed
    # but redemption is skipped (the real-target solver doesn't terminate).
    benchmark("genesis", ["--height", "0"],
              synthetic_target="0x000fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff")
    benchmark("recent_easy", ["--height", "100000"])
    benchmark("current_tip", ["--tip"])
