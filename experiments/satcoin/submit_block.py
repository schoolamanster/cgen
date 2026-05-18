"""Verify a SAT solver's nonce and (optionally) submit the block.

This is the redemption path. Run it on a solver's `v ...` output to:

  1. Extract the nonce bits from the assignment (CNF₁ variables 1..32).
  2. Reconstruct the 80-byte header with that nonce.
  3. Recompute double-SHA-256 with Python's hashlib (the canonical
     implementation — independent of cgen's CNF encoding, so this
     catches any encoding bug).
  4. Compare against the difficulty constraint to confirm the solver
     didn't lie or that we didn't parse wrong.
  5. If a Bitcoin Core node is configured AND the hash actually meets
     the *real* network target (not just our academic top-N-bits-zero),
     prepare a submitblock call.

In practice this script's job is mostly verification. The submission half
only runs if all of these hold simultaneously:
  - The block is unmined (--live-template mode).
  - The hash meets the live network target.
  - bitcoin-cli is on PATH and the local node is synced.

Usage:
    # Solver wrote its output to solver.log
    python submit_block.py --header out/header.json \\
        --solver-output solver.log \\
        [--difficulty-bits 8]

    # Or feed solver output via stdin:
    cryptominisat5 out/satcoin.cnf | python submit_block.py --header out/header.json -
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path


def parse_solver_assignment(text: str) -> dict[int, bool]:
    """Parse DIMACS `v` lines into a {var_number: bool} mapping."""
    assignment = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("v "):
            for tok in line[2:].split():
                v = int(tok)
                if v == 0:
                    continue
                assignment[abs(v)] = v > 0
    return assignment


def extract_nonce(assignment: dict[int, bool]) -> int:
    """Recover the 32-bit nonce from variables 1..32.

    The satcoin orchestrator places the 4 nonce bytes as cgen's M bits
    609..640, which become CNF variables 1..32 (MSB-first within each byte,
    bytes in header order — i.e. var 1 is the MSB of header byte 76).
    """
    missing = [i for i in range(1, 33) if i not in assignment]
    if missing:
        sys.exit(f"Solver assignment missing nonce variables: {missing}")
    # Build the 32 bits MSB-first, then interpret as a big-endian 32-bit
    # number. That gives us a "word in SHA-256 byte order" — i.e. the four
    # bytes 76,77,78,79 of the header in network order.
    bits = "".join("1" if assignment[i] else "0" for i in range(1, 33))
    word_be = int(bits, 2)
    return word_be


def reconstruct_header(header_hex: str, nonce_be: int) -> bytes:
    """Replace bytes 76-79 of the header hex with the recovered nonce bytes.

    `nonce_be` is the 32-bit integer formed by bytes 76,77,78,79 read in
    network order (the order in which SHA-256 sees them). It's NOT the
    Bitcoin "nonce" field interpreted as a little-endian integer.
    """
    raw = bytearray.fromhex(header_hex)
    raw[76:80] = nonce_be.to_bytes(4, "big")
    return bytes(raw)


def double_sha256(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def leading_zero_bits_be(data: bytes) -> int:
    """Count zero bits from the MSB of `data` (treating data as big-endian).

    Used to check the *academic* constraint encoded by build_satcoin_cnf.py:
    the top N bits of SHA-256's BE output must be zero.
    """
    count = 0
    for byte in data:
        if byte == 0:
            count += 8
            continue
        for shift in range(7, -1, -1):
            if (byte >> shift) & 1:
                return count
            count += 1
        return count
    return count


def meets_bitcoin_target(hash_bytes: bytes, target_hex: str) -> bool:
    """Check the *real* Bitcoin rule: byte-reversed hash, interpreted as
    a 256-bit integer, must be ≤ target.
    """
    target = int(target_hex, 16)
    # Bitcoin displays/compares the hash byte-reversed relative to SHA-256's
    # native output.
    reversed_int = int.from_bytes(hash_bytes[::-1], "big")
    return reversed_int <= target


def maybe_submit(block_hex: str) -> None:
    """If bitcoin-cli is on PATH, print the submit command and ask before running.

    This script will not auto-submit anything — submission of a real block
    is the kind of thing you don't do in an `os.system` call without
    eyeballs on it.
    """
    cli = shutil.which("bitcoin-cli")
    if not cli:
        print(
            "\n[submit] No bitcoin-cli on PATH — submission step skipped.\n"
            "         To submit a found block, you'd run:\n"
            f"         bitcoin-cli submitblock {block_hex[:32]}...{block_hex[-16:]}",
            file=sys.stderr,
        )
        return
    print(
        "\n[submit] bitcoin-cli detected. To submit:\n"
        f"         {cli} submitblock <full block hex>\n"
        "         Note: only the header is reconstructed here. A real submission\n"
        "         needs the full block (header + transactions) matching the\n"
        "         merkle root committed in the header. That requires a node\n"
        "         template (`getblocktemplate`) the header came from.\n"
        "         This script does NOT auto-run submitblock.",
        file=sys.stderr,
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--header", required=True, type=Path,
                   help="The header JSON used to build the CNF.")
    p.add_argument("--solver-output", required=True,
                   help="Path to file containing solver output, or '-' for stdin.")
    p.add_argument("--difficulty-bits", type=int,
                   help="The N used when building the CNF (for verification).")
    args = p.parse_args()

    with open(args.header) as f:
        header = json.load(f)

    if args.solver_output == "-":
        solver_text = sys.stdin.read()
    else:
        with open(args.solver_output) as f:
            solver_text = f.read()

    if "s SATISFIABLE" not in solver_text:
        # Common cases: UNSAT (proven no nonce exists for this target — only
        # happens for impossibly-tight constraints), or solver timed out.
        verdict = "UNKNOWN"
        for line in solver_text.splitlines():
            if line.startswith("s "):
                verdict = line.strip()
                break
        sys.exit(f"Solver did not report SATISFIABLE. Found: {verdict}")

    assignment = parse_solver_assignment(solver_text)
    nonce_be = extract_nonce(assignment)
    reconstructed = reconstruct_header(header["raw_header_hex"], nonce_be)
    h = double_sha256(reconstructed)

    nonce_le_int = int.from_bytes(nonce_be.to_bytes(4, "big")[::-1], "big")
    leading_zeros = leading_zero_bits_be(h)
    displayed_hash = h[::-1].hex()

    print(json.dumps({
        "nonce_bytes_network_order_hex": nonce_be.to_bytes(4, "big").hex(),
        "nonce_as_little_endian_int": nonce_le_int,  # how block explorers display the nonce
        "reconstructed_header_hex": reconstructed.hex(),
        "double_sha256_raw_hex": h.hex(),
        "double_sha256_displayed_hex": displayed_hash,
        "leading_zero_bits_in_BE_hash": leading_zeros,
        "meets_real_bitcoin_target": meets_bitcoin_target(h, header["fields"]["target"]),
    }, indent=2))

    if args.difficulty_bits is not None:
        if leading_zeros < args.difficulty_bits:
            sys.exit(
                f"\n[verify] FAIL: encoding asserted top {args.difficulty_bits} bits = 0, "
                f"but recovered hash has only {leading_zeros} leading zero bits in BE.\n"
                "         This means the splice/encoding has a bug — debug before trusting.",
            )
        print(f"\n[verify] OK: hash has {leading_zeros} leading zero bits, "
              f"satisfying the encoded constraint of ≥ {args.difficulty_bits}.",
              file=sys.stderr)

    # Real submission path is dead code in our experiment but let's at least
    # print what would happen if someone wanted to try.
    if meets_bitcoin_target(h, header["fields"]["target"]):
        print("\n[!] This hash meets the REAL Bitcoin target for this block.", file=sys.stderr)
        maybe_submit(reconstructed.hex())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
