"""Verify our pipeline against the most recently-mined Bitcoin block.

This is the repeatable, no-inline-arithmetic version of the pop-quiz I ran
ad-hoc earlier. It uses the central nonce_pinning helpers so the
test-only byte-order mistake (LE integer vs. network-order bytes) cannot
be reintroduced.

Flow:
  1. Query local Bitcoin Core for the current chain tip via RPC.
  2. Fetch that block's exact 80-byte header.
  3. Build a satcoin CNF for that header (using the same pipeline live
     mining would use).
  4. Pin the actual winning nonce as 32 unit clauses (extracted from the
     raw header bytes — the only correct source).
  5. Run CryptoMiniSat. Must return SAT.
  6. Confirm the SAT assignment's first 32 variables match the expected
     pin (sanity check on the solver's output parsing path).

Output: a small JSON summary + a PASS/FAIL line. Exits non-zero on FAIL.

Run:
    python verify_recent_winner.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from block_template import rpc  # noqa: E402
from nonce_pinning import (  # noqa: E402
    nonce_bytes_from_header,
    unit_clauses_for_nonce,
    pin_nonce_in_cnf,
    nonce_int_from_assignment,
)

CMS = HERE.parent.parent / "tools" / "cryptominisat" / "cryptominisat5.exe"
OUT = HERE / "out" / "recent_winner"
OUT.mkdir(parents=True, exist_ok=True)


def fetch_header_json(block_hash: str) -> dict:
    """Use fetch_block.py --hash to get the canonical header in our JSON shape."""
    res = subprocess.run(
        [sys.executable, str(HERE / "fetch_block.py"),
         "--hash", block_hash, "--rpc"],
        capture_output=True, text=True, cwd=HERE,
    )
    if res.returncode != 0:
        sys.exit(f"fetch_block.py failed: {res.stderr[:300]}")
    return json.loads(res.stdout)


def build_cnf(header_path: Path, cnf_path: Path) -> tuple[int, int]:
    """Use build_satcoin_cnf.py to encode the header. Returns (n_vars, n_clauses)."""
    res = subprocess.run(
        [sys.executable, str(HERE / "build_satcoin_cnf.py"),
         "--header", str(header_path),
         "--output", str(cnf_path)],
        capture_output=True, text=True, cwd=HERE,
    )
    if res.returncode != 0:
        sys.exit(f"build_satcoin_cnf.py failed: {res.stderr[:300]}")
    with open(cnf_path) as f:
        for line in f:
            if line.startswith("p cnf"):
                _, _, n_vars, n_clauses = line.split()
                return int(n_vars), int(n_clauses)
    sys.exit("no p cnf header in CNF")


def solve(cnf_path: Path, timeout_s: int = 120) -> tuple[str, str]:
    """Run CMS. Returns (verdict, full output)."""
    res = subprocess.run([str(CMS), "--verb", "0", str(cnf_path)],
                         capture_output=True, text=True, timeout=timeout_s)
    verdict = "?"
    for line in res.stdout.splitlines():
        if line.startswith("s "):
            verdict = line.strip()
            break
    return verdict, res.stdout


def parse_assignment(solver_text: str) -> dict[int, bool]:
    a = {}
    for line in solver_text.splitlines():
        if line.startswith("v "):
            for tok in line[2:].split():
                v = int(tok)
                if v == 0:
                    continue
                a[abs(v)] = v > 0
    return a


def main() -> int:
    # 1. Get current chain tip.
    tip_hash = rpc("getbestblockhash")
    chain = rpc("getblockchaininfo")
    tip_height = chain["blocks"]
    print(f"[1/6] Most recent block: height {tip_height:,}  hash {tip_hash[:24]}...")

    # 2. Fetch its header.
    header = fetch_header_json(tip_hash)
    header_path = OUT / "header.json"
    header_path.write_text(json.dumps(header))
    raw_hex = header["raw_header_hex"]
    nonce_displayed = header["fields"]["nonce"]
    target = header["fields"]["target"]
    print(f"[2/6] Header: {len(raw_hex)//2} bytes, "
          f"displayed nonce={nonce_displayed:,}, target={target[:18]}...")

    # 3. Build the CNF.
    cnf_path = OUT / "winner.cnf"
    t0 = time.perf_counter()
    n_vars, n_cls = build_cnf(header_path, cnf_path)
    print(f"[3/6] CNF built: {n_vars:,} vars, {n_cls:,} clauses "
          f"in {(time.perf_counter()-t0)*1000:.0f} ms")

    # 4. Pin the nonce via the single-source-of-truth helper. This is where
    #    the byte-order mistake used to live; using the helper makes it
    #    structurally impossible to repeat.
    pinned_path = OUT / "winner_pinned.cnf"
    n_units = pin_nonce_in_cnf(str(cnf_path), str(pinned_path), raw_hex)
    nonce_bytes_for_log = nonce_bytes_from_header(raw_hex).hex()
    units_preview = unit_clauses_for_nonce(nonce_bytes_from_header(raw_hex))
    print(f"[4/6] Pinned {n_units} nonce unit clauses (network-order bytes {nonce_bytes_for_log})")
    print(f"      first 8 units: {' '.join(f'{u:+d}' for u in units_preview[:8])}")

    # 5. Solve. Must be SAT — if it's not, the pipeline has a real bug.
    t0 = time.perf_counter()
    verdict, solver_text = solve(pinned_path)
    solve_ms = (time.perf_counter() - t0) * 1000
    print(f"[5/6] Solver: {verdict} in {solve_ms:.0f} ms")
    if verdict != "s SATISFIABLE":
        print(f"FAIL: expected SATISFIABLE, got '{verdict}'")
        print("This means either the pipeline encoding has a bug OR our pinning is wrong.")
        return 1

    # 6. Inverse check: extract the nonce from the SAT assignment and confirm
    #    it round-trips to the same displayed integer.
    assignment = parse_assignment(solver_text)
    recovered = nonce_int_from_assignment(assignment)
    ok_recover = recovered == nonce_displayed
    print(f"[6/6] Recovered nonce from solver assignment: {recovered:,}")
    print(f"      Expected (chain header):                 {nonce_displayed:,}")
    print(f"      Match: {ok_recover}")

    summary = {
        "block_height": tip_height,
        "block_hash": tip_hash,
        "displayed_nonce": nonce_displayed,
        "header_bytes_76_79_hex": nonce_bytes_for_log,
        "cnf_size": {"vars": n_vars, "clauses": n_cls},
        "verdict": verdict,
        "solve_ms": round(solve_ms, 1),
        "recovered_nonce_from_assignment": recovered,
        "all_32_bits_match": ok_recover,
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2))
    print()
    if verdict == "s SATISFIABLE" and ok_recover:
        print(f"PASS: pipeline correctly verifies block {tip_height:,}'s real winning nonce.")
        return 0
    print("FAIL: see above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
