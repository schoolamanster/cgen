"""End-to-end test suite for the satcoin pipeline.

The premise: every Bitcoin block ever mined is a known-solution to its own
SAT instance. Feed our pipeline a real header, encode it with our exact
'hash ≤ target' rule, and verify by:

  1. Pinning the KNOWN nonce as unit clauses → solver must return SAT.
     Catches false-negative bugs: encoding rejects a valid Bitcoin solution
     (most commonly an endianness flip somewhere that makes the constraint
     look unmet from the solver's perspective).

  2. Pinning a KNOWN-WRONG nonce as unit clauses → solver must return UNSAT.
     Catches false-positive bugs: encoding accepts a non-Bitcoin solution
     (most commonly endianness flips that make a too-large hash appear
     small or vice versa). This is the most important test for endianness
     correctness — if the encoding is byte-reversed wrong, this would
     silently pass.

  3. Redemption roundtrip: construct a synthetic SAT assignment with the
     known nonce bits, feed it through submit_block.py, verify that
     hashlib's independent recomputation says it meets the REAL Bitcoin
     target. Catches bugs in nonce extraction, header reconstruction,
     and the byte order in submit_block.py.

Run:
    python tests.py

Exit code is 0 on all-pass, 1 on any failure. Each test runs in seconds —
the CNF is large but with the nonce fully pinned, propagation is
deterministic (no backtracking).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
CMS = REPO_ROOT / "tools" / "cryptominisat" / "cryptominisat5.exe"
OUT = HERE / "out" / "tests"
OUT.mkdir(parents=True, exist_ok=True)

# Blocks under test. Each is a stable, well-known historical block where
# the nonce is publicly verified. We fetch the header at runtime to keep
# the test data live.
KNOWN_BLOCKS = [
    {"label": "genesis", "height": 0},
    {"label": "block_1", "height": 1},
    {"label": "block_100k", "height": 100000},
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_py(script: str, *args: str) -> subprocess.CompletedProcess:
    res = subprocess.run([sys.executable, str(HERE / script), *args],
                         capture_output=True, text=True, cwd=HERE)
    if res.returncode != 0:
        print(res.stdout); print(res.stderr, file=sys.stderr)
        raise RuntimeError(f"{script} failed: {' '.join(args)}")
    return res


def fetch_header(height: int) -> dict:
    res = run_py("fetch_block.py", "--height", str(height))
    return json.loads(res.stdout)


def build_cnf(header_path: Path, cnf_path: Path) -> None:
    run_py("build_satcoin_cnf.py",
           "--header", str(header_path),
           "--output", str(cnf_path))


def nonce_to_unit_clauses(nonce_bytes: bytes) -> list[int]:
    """Map 4 nonce bytes (in network/SHA-256 order, i.e. header bytes 76..79)
    to 32 signed-int unit clauses pinning CNF variables 1..32.

    The variable layout (set by cgen's encoding with except:609..640):
        var 1  = bit 1 of M = MSB of header byte 76 (= MSB of bit-0 of nonce_bytes[0])
        var 8  = LSB of header byte 76
        var 9  = MSB of header byte 77
        ...
        var 32 = LSB of header byte 79

    So bit order is MSB-first within each byte, bytes in header order.
    """
    if len(nonce_bytes) != 4:
        raise ValueError("Expected 4 nonce bytes")
    units: list[int] = []
    for byte_i, b in enumerate(nonce_bytes):
        for bit_in_byte in range(7, -1, -1):  # MSB first
            var = byte_i * 8 + (7 - bit_in_byte) + 1
            sign = 1 if (b >> bit_in_byte) & 1 else -1
            units.append(sign * var)
    assert len(units) == 32
    return units


def append_units(cnf_in: Path, cnf_out: Path, units: list[int]) -> None:
    """Write a copy of cnf_in to cnf_out with `len(units)` extra unit clauses."""
    n_vars = n_cls = None
    with open(cnf_in) as f, open(cnf_out, "w") as o:
        for line in f:
            if line.startswith("p cnf") and n_vars is None:
                parts = line.split()
                n_vars, n_cls = int(parts[2]), int(parts[3])
                o.write(f"p cnf {n_vars} {n_cls + len(units)}\n")
            else:
                o.write(line)
        for u in units:
            o.write(f"{u} 0\n")


def solve(cnf_path: Path, timeout_s: int = 120) -> str:
    """Run CryptoMiniSat. Returns 's SATISFIABLE' / 's UNSATISFIABLE' / 'TIMEOUT'."""
    try:
        res = subprocess.run([str(CMS), "--verb", "0", str(cnf_path)],
                             capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return "TIMEOUT"
    for line in res.stdout.splitlines():
        if line.startswith("s "):
            return line.strip()
    return "NO_VERDICT"


def double_sha256(b: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


def meets_target(hash_bytes: bytes, target_hex: str) -> bool:
    if target_hex.startswith("0x"):
        target_hex = target_hex[2:]
    return int.from_bytes(hash_bytes[::-1], "big") <= int(target_hex, 16)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_known_nonce_accepted(block: dict, cnf_path: Path, header: dict) -> tuple[bool, str]:
    """Pin the real nonce; encoding must be satisfiable."""
    nonce_bytes = bytes.fromhex(header["raw_header_hex"][152:160])
    units = nonce_to_unit_clauses(nonce_bytes)
    pinned = OUT / f"{block['label']}_known_nonce.cnf"
    append_units(cnf_path, pinned, units)
    t0 = time.perf_counter()
    verdict = solve(pinned, timeout_s=120)
    dt = time.perf_counter() - t0
    ok = verdict == "s SATISFIABLE"
    detail = f"verdict={verdict} in {dt*1000:.0f} ms (nonce={nonce_bytes.hex()})"
    return ok, detail


def test_wrong_nonce_rejected(block: dict, cnf_path: Path, header: dict) -> tuple[bool, str]:
    """Pin a nonce we've verified produces an invalid hash; encoding must be UNSAT.

    This is the endianness canary. If the byte-reversal in the target-cascade
    is flipped, a hash that's > target will look like ≤ target to the encoding
    and the wrong nonce will incorrectly be accepted as SAT.
    """
    correct_nonce_be = bytes.fromhex(header["raw_header_hex"][152:160])
    # Find a nonce that produces a hash NOT meeting the real target.
    # Start with +1 and walk forward — at genesis difficulty most nonces are
    # invalid (1 in ~65k is valid), so we'll find one quickly.
    correct_int = int.from_bytes(correct_nonce_be, "big")
    raw_prefix = bytes.fromhex(header["raw_header_hex"][:152])
    target_hex = header["fields"]["target"]
    wrong_int = (correct_int + 1) & 0xFFFFFFFF
    for _ in range(1000):
        wrong_bytes = wrong_int.to_bytes(4, "big")
        full_header = raw_prefix + wrong_bytes
        h = double_sha256(full_header)
        if not meets_target(h, target_hex):
            break
        wrong_int = (wrong_int + 1) & 0xFFFFFFFF
    else:
        return False, "could not find a wrong nonce (every probe satisfied the target — suspicious)"

    units = nonce_to_unit_clauses(wrong_bytes)
    pinned = OUT / f"{block['label']}_wrong_nonce.cnf"
    append_units(cnf_path, pinned, units)
    t0 = time.perf_counter()
    verdict = solve(pinned, timeout_s=120)
    dt = time.perf_counter() - t0
    ok = verdict == "s UNSATISFIABLE"
    detail = (f"verdict={verdict} in {dt*1000:.0f} ms "
              f"(wrong_nonce={wrong_bytes.hex()}, hashlib confirms invalid)")
    return ok, detail


def test_redemption_roundtrip(block: dict, cnf_path: Path, header: dict) -> tuple[bool, str]:
    """Construct a synthetic SAT log with the known nonce bits and feed it
    through submit_block.py. The redemption code must report the recovered
    nonce satisfies the real Bitcoin target."""
    nonce_bytes = bytes.fromhex(header["raw_header_hex"][152:160])
    units = nonce_to_unit_clauses(nonce_bytes)
    fake_v = " ".join(str(u) for u in units)
    # The solver also needs to "assign" something (positive or negative)
    # to every other variable for submit_block.py to be happy. We give
    # arbitrary signs for the rest; submit_block.py only cares about vars 1..32.
    n_vars = None
    with open(cnf_path) as f:
        for line in f:
            if line.startswith("p cnf"):
                n_vars = int(line.split()[2])
                break
    extra = " ".join(str(v) for v in range(33, n_vars + 1))
    synthetic_log = f"s SATISFIABLE\nv {fake_v} {extra} 0\n"
    log_path = OUT / f"{block['label']}_synthetic_solver.log"
    log_path.write_text(synthetic_log)
    # Save the header to a file path (submit_block reads it from disk).
    header_path = OUT / f"{block['label']}_header.json"
    header_path.write_text(json.dumps(header))

    res = subprocess.run(
        [sys.executable, str(HERE / "submit_block.py"),
         "--header", str(header_path),
         "--solver-output", str(log_path)],
        capture_output=True, text=True,
    )
    out = res.stdout + res.stderr
    if "meets_real_bitcoin_target" not in out:
        return False, f"submit_block.py output missing field. stdout={res.stdout[:200]}"
    # Parse the JSON output.
    try:
        # Output has a JSON object followed by stderr-style verify lines.
        # Find the JSON braces span.
        start = res.stdout.index("{"); end = res.stdout.rindex("}") + 1
        payload = json.loads(res.stdout[start:end])
    except (ValueError, json.JSONDecodeError) as e:
        return False, f"could not parse submit_block.py JSON: {e}"
    ok = payload.get("meets_real_bitcoin_target") is True
    detail = (f"meets_real_bitcoin_target={payload.get('meets_real_bitcoin_target')}, "
              f"hash={payload.get('double_sha256_displayed_hex','?')[:16]}...")
    return ok, detail


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_suite() -> int:
    print(f"{'BLOCK':<14} {'TEST':<40} {'STATUS':<6}  DETAIL")
    print("-" * 100)
    all_pass = True
    for block in KNOWN_BLOCKS:
        # Fetch + build CNF once per block.
        try:
            header = fetch_header(block["height"])
        except RuntimeError as e:
            print(f"{block['label']:<14} {'(setup: fetch)':<40} {'FAIL':<6}  {e}")
            all_pass = False
            continue
        header_path = OUT / f"{block['label']}_header.json"
        header_path.write_text(json.dumps(header))
        cnf_path = OUT / f"{block['label']}.cnf"
        try:
            build_cnf(header_path, cnf_path)
        except RuntimeError as e:
            print(f"{block['label']:<14} {'(setup: build_cnf)':<40} {'FAIL':<6}  {e}")
            all_pass = False
            continue

        for test_name, test_fn in (
            ("known nonce accepted (SAT)", test_known_nonce_accepted),
            ("wrong nonce rejected (UNSAT)", test_wrong_nonce_rejected),
            ("redemption roundtrip", test_redemption_roundtrip),
        ):
            try:
                ok, detail = test_fn(block, cnf_path, header)
            except Exception as e:
                ok = False
                detail = f"exception: {type(e).__name__}: {e}"
            status = "PASS" if ok else "FAIL"
            if not ok:
                all_pass = False
            print(f"{block['label']:<14} {test_name:<40} {status:<6}  {detail}")
    print("-" * 100)
    print("ALL PASS" if all_pass else "SOME FAILED")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(run_suite())
