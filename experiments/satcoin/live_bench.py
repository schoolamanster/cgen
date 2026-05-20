"""End-to-end live mining benchmark, run once against the live network.

Exercises the FULL pipeline against a real getblocktemplate response and
reports per-phase wall clock. The 'solver' is the 0-second oracle of the
academic premise — we substitute a synthetic SATISFIABLE log built from
the template's placeholder nonce.

Phases:
  1. fetch live template (block_template.py)
  2. build satcoin CNF (build_satcoin_cnf.py)
  3. parse + verify + (would-be submit) (submit_block.py without --submit)
  4. extras: write synthetic solver log (negligible, but counted)

Run:
    python live_bench.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out" / "live"
OUT.mkdir(parents=True, exist_ok=True)


def time_step(label: str, fn):
    t0 = time.perf_counter()
    result = fn()
    dt = (time.perf_counter() - t0) * 1000
    print(f"  {label:<42} {dt:>8.1f} ms")
    return dt, result


def run_py(*args, **kwargs):
    res = subprocess.run([sys.executable, *args], capture_output=True, text=True, cwd=HERE, **kwargs)
    if res.returncode != 0:
        sys.stderr.write(res.stdout); sys.stderr.write(res.stderr)
        raise SystemExit(f"command failed: {args}")
    return res


def main():
    print(f"\nLive end-to-end satcoin pipeline benchmark")
    print("-" * 70)

    header_path = OUT / "live_header.json"
    cnf_path = OUT / "live_sat.cnf"
    log_path = OUT / "live_solver.log"

    # 1. fetch live template + assemble candidate block
    t_fetch, _ = time_step("1. fetch + assemble live template", lambda:
        header_path.write_text(run_py("block_template.py").stdout))
    header = json.loads(header_path.read_text())

    # 2. build the CNF
    t_build, _ = time_step("2. build satcoin CNF (cgen x2 + splice + target)", lambda:
        run_py("build_satcoin_cnf.py",
               "--header", str(header_path),
               "--output", str(cnf_path)))
    n_vars = n_cls = None
    with open(cnf_path) as f:
        for line in f:
            if line.startswith("p cnf"):
                _, _, n_vars, n_cls = line.split()
                break

    # 3. write a synthetic 0-second-solver log using the placeholder nonce (0).
    #    We're timing redemption, not actually trying to broadcast.
    def make_synthetic_log():
        # Use the template's current nonce (placeholder = 0) as the "solver result".
        # This produces a hash that won't meet the real target, but the code path
        # exercised by submit_block.py up to the verify step is identical to a
        # real winning solve.
        header_hex = header["raw_header_hex"]
        nonce_bytes = bytes.fromhex(header_hex[152:160])
        units = []
        for byte_i, b in enumerate(nonce_bytes):
            for bit in range(7, -1, -1):
                var = byte_i * 8 + (7 - bit) + 1
                units.append((1 if (b >> bit) & 1 else -1) * var)
        rest = " ".join(str(v) for v in range(33, int(n_vars) + 1))
        log_path.write_text(
            f"s SATISFIABLE\nv {' '.join(str(u) for u in units)} {rest} 0\n")
    t_synth, _ = time_step("3. synthesize 0-second solver log", make_synthetic_log)

    # 4. redemption: parse + reconstruct + hashlib verify.
    #    submit_block.py exits non-zero on verify failure (placeholder nonce won't
    #    satisfy mainnet target). For timing we capture and continue.
    def redeem():
        res = subprocess.run(
            [sys.executable, str(HERE / "submit_block.py"),
             "--header", str(header_path),
             "--solver-output", str(log_path)],
            capture_output=True, text=True, cwd=HERE,
        )
        return res

    t_redeem, res = time_step("4. redemption (parse + reconstruct + verify)", redeem)

    print("-" * 70)
    total = t_fetch + t_build + t_synth + t_redeem
    print(f"  {'TOTAL pipeline wall-clock':<42} {total:>8.1f} ms  ({total/1000:.2f} s)")
    print()
    print(f"  CNF size:                   {n_vars} vars, {n_cls} clauses")
    print(f"  Block height being mined:   {header['block_height']}")
    print(f"  Coinbase value:             {header['coinbase_value_btc']} BTC")
    print(f"  Payout script:              {header.get('payout_script_hex','?')[:32]}...")
    print(f"  Tx count:                   {header['extras']['tx_count_total']}")
    print(f"  Target:                     {header['fields']['target']}")
    print(f"  Live template fetched:      {header['fields']['timestamp']} unix")

    if "meets_real_bitcoin_target" in res.stdout:
        # Parse the verdict to confirm code paths were exercised.
        import re
        m = re.search(r'"meets_real_bitcoin_target":\s*(true|false)', res.stdout)
        v = m.group(1) if m else "?"
        print(f"  Verdict (placeholder nonce): meets_real_bitcoin_target = {v}  "
              f"(expected false; we didn't solve)")

    print()
    print("All phases executed against the LIVE network. The solver step is")
    print("the only thing absent — under the 0-second-solver premise, this is")
    print(f"the actual end-to-end mining latency: {total/1000:.2f} s per attempt.")


if __name__ == "__main__":
    main()
