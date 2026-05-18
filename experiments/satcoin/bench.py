"""Time the optimized phases: fetch, conversion-to-SAT, and redemption.

This benchmark measures the parts we control. Solver time is intentionally
excluded — under the "0-second solver" academic premise, the solver is
treated as an oracle. We construct a *synthetic* SAT log (using the
block's KNOWN nonce) and feed it into the redemption phase. That way:

  - Every block's redemption can be timed, not just artificially-easy ones.
  - The timing reflects only the parse + hashlib verify work.
  - The bench runs in seconds, with no dependence on solver behavior.

Phases reported (per block):
  1. fetch         — HTTP call + local hash verification (fetch_block.py)
  2. conversion    — cgen ×2 + parse + splice + target cascade (build_satcoin_cnf.py)
  3. redemption    — synthetic solver-log → parse + hashlib verify (submit_block.py)

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


def make_synthetic_solver_log(header: dict, cnf_path: Path) -> Path:
    """Construct a synthetic SATISFIABLE log using the block's known nonce.

    This stand-in for a real solver lets us time the redemption phase
    without the (academically-intractable) solver in the loop.
    """
    nonce_bytes = bytes.fromhex(header["raw_header_hex"][152:160])
    units = []
    for byte_i, b in enumerate(nonce_bytes):
        for bit_in_byte in range(7, -1, -1):
            var = byte_i * 8 + (7 - bit_in_byte) + 1
            sign = 1 if (b >> bit_in_byte) & 1 else -1
            units.append(sign * var)
    n_vars = None
    with open(cnf_path) as f:
        for line in f:
            if line.startswith("p cnf"):
                n_vars = int(line.split()[2])
                break
    log_path = OUT / f"synthetic_{cnf_path.stem}.log"
    rest = " ".join(str(v) for v in range(33, n_vars + 1))
    log_path.write_text(f"s SATISFIABLE\nv {' '.join(str(u) for u in units)} {rest} 0\n")
    return log_path


def benchmark(label: str, fetch_args: list[str]):
    print(f"\n=== {label} ===")
    header_path = OUT / f"header_{label}.json"
    cnf_path = OUT / f"satcoin_{label}.cnf"

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

    # Synthesize a "0-second solver" output and time the redemption path.
    log_path = make_synthetic_solver_log(header, cnf_path)
    with time_block("redemption (parse + verify)           ") as t:
        run("submit_block.py",
            "--header", str(header_path),
            "--solver-output", str(log_path))


if __name__ == "__main__":
    benchmark("genesis", ["--height", "0"])
    benchmark("block_100k", ["--height", "100000"])
    benchmark("current_tip", ["--tip"])
