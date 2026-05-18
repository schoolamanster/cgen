"""Verify a SAT solver's nonce and (optionally) submit the block.

This is the redemption path. Run it on a solver's `v ...` output to:

  1. Extract the nonce bits from the assignment (CNF₁ variables 1..32).
  2. Reconstruct the 80-byte header with that nonce.
  3. Recompute double-SHA-256 with Python's hashlib (the canonical
     implementation — independent of cgen's CNF encoding, so this
     catches any encoding bug).
  4. Compare against the network target.
  5. With --submit, broadcast via local Bitcoin Core RPC submitblock.
     The script verifies first; only broadcasts if hash meets the real
     target AND the user passed --submit explicitly (no auto-broadcast).

Usage:
    # Just verify
    python submit_block.py --header out/header.json --solver-output sol.log

    # Verify + (if winning) broadcast via local node
    python submit_block.py --header out/header.json --solver-output sol.log --submit

    # Stream from a live solver
    cryptominisat5 out/satcoin.cnf | python submit_block.py --header out/header.json -
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

RPC_URL = "http://127.0.0.1:8332/"


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


def _rpc_creds() -> tuple[str, str]:
    """Read rpcuser/rpcpassword from bitcoin.conf."""
    conf_path = os.path.join(os.environ.get("APPDATA", ""), "Bitcoin", "bitcoin.conf")
    if not os.path.exists(conf_path):
        sys.exit(f"--submit requires {conf_path} with rpcuser/rpcpassword set")
    user = pw = None
    with open(conf_path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("rpcuser="):
                user = line.split("=", 1)[1]
            elif line.startswith("rpcpassword="):
                pw = line.split("=", 1)[1]
    if not (user and pw):
        sys.exit(f"rpcuser or rpcpassword missing from {conf_path}")
    return user, pw


def rpc_call(method: str, params: list):
    """Single JSON-RPC POST. Returns the result field (or raises on RPC error)."""
    user, pw = _rpc_creds()
    auth = base64.b64encode(f"{user}:{pw}".encode()).decode()
    payload = json.dumps({"jsonrpc": "1.0", "id": "satcoin", "method": method, "params": params}).encode()
    req = urllib.request.Request(
        RPC_URL,
        data=payload,
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
            return {"error": body.get("error", f"HTTP {e.code}"), "result": None}
        except Exception:
            return {"error": f"HTTP {e.code}", "result": None}
    except urllib.error.URLError as e:
        return {"error": f"unreachable ({RPC_URL}): {e.reason}", "result": None}
    return body


def submit_via_rpc(block_hex: str) -> dict:
    """Call submitblock via local RPC. Returns {'status', 'detail'}.

    submitblock semantics (per Bitcoin Core):
      - result = null and no error → accepted by the network
      - result = string error code → rejected (e.g. "high-hash" if hash > target,
        "bad-prevblk" if prev hash doesn't match the current tip, etc.)
      - HTTP/RPC error → couldn't even reach the node
    """
    resp = rpc_call("submitblock", [block_hex])
    if resp.get("error"):
        return {"status": "rpc_error", "detail": resp["error"]}
    result = resp.get("result")
    if result is None:
        return {"status": "accepted", "detail": "node accepted the block; gossiped to peers"}
    return {"status": "rejected", "detail": f"rejection reason: {result}"}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--header", required=True, type=Path,
                   help="The header JSON used to build the CNF.")
    p.add_argument("--solver-output", required=True,
                   help="Path to file containing solver output, or '-' for stdin.")
    p.add_argument("--target", type=str, default=None,
                   help="Target the CNF was built against (default: read from header). "
                        "If you used --target when building the CNF, pass the same value here.")
    p.add_argument("--submit", action="store_true",
                   help="If the hash meets the real Bitcoin target, broadcast via local "
                        "Bitcoin Core RPC. Requires bitcoind running, fully synced, and "
                        "the block to be a valid extension of the current chain tip. "
                        "Without --submit, the script only verifies and prints what it WOULD do.")
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

    # Verify against the target the CNF was actually built with.
    target_used = args.target if args.target else header["fields"]["target"]
    meets_encoded = meets_bitcoin_target(h, target_used)
    meets_real = meets_bitcoin_target(h, header["fields"]["target"])

    print(json.dumps({
        "nonce_bytes_network_order_hex": nonce_be.to_bytes(4, "big").hex(),
        "nonce_as_little_endian_int": nonce_le_int,  # how block explorers display the nonce
        "reconstructed_header_hex": reconstructed.hex(),
        "double_sha256_raw_hex": h.hex(),
        "double_sha256_displayed_hex": displayed_hash,
        "leading_zero_bits_in_BE_hash": leading_zeros,
        "target_used_for_encoding": target_used,
        "meets_encoded_target": meets_encoded,
        "meets_real_bitcoin_target": meets_real,
    }, indent=2))

    if not meets_encoded:
        sys.exit(
            f"\n[verify] FAIL: encoded constraint was hash <= {target_used}, "
            f"but hashlib-recomputed hash does NOT satisfy it.\n"
            "         This means the CNF splice/encoding has a bug — debug before trusting."
        )
    print("\n[verify] OK: hashlib confirms the solver's nonce satisfies the encoded target.",
          file=sys.stderr)

    if meets_real:
        print("\n[!] This hash also meets the REAL Bitcoin target for this block.", file=sys.stderr)
        if args.submit:
            # Submission via RPC. Note: this submits just the 80-byte header,
            # which the node will reject unless followed by a properly-formed
            # full block. A real "we found a block" flow needs the full block
            # hex (header + coinbase tx + other txns matching the merkle root)
            # — typically obtained from getblocktemplate. The hook below is
            # ready for that hex; we currently only have the header.
            print("[submit] Calling submitblock via local Bitcoin Core RPC...", file=sys.stderr)
            result = submit_via_rpc(reconstructed.hex())
            print(f"[submit] {result['status']}: {result['detail']}", file=sys.stderr)
            if result["status"] != "accepted":
                # Most likely reason for a header-only submit: node rejects
                # because there's no body. That's expected without a full
                # block template. We surface the rejection so it's not silent.
                sys.exit(1)
        else:
            print(
                "[submit] --submit not passed; not broadcasting. To submit:\n"
                "         python submit_block.py ... --submit\n"
                "         Caveat: submitblock needs the full block hex (header + txns),\n"
                "         not just the header. A real mining flow constructs that from\n"
                "         getblocktemplate; see TODO in README §8.",
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
