"""End-to-end test of steps 11 and 12 of the live mining pipeline,
using a real historical block as the test vector.

Steps under test:
  11. submit_block.py reconstructs the 80-byte header from a SAT-solver's
      nonce assignment and splices it into the full-block body bytes.
  12. submit_block.py calls bitcoin-cli submitblock RPC with the assembled
      block hex.

Test strategy: pick a real validated block from local RPC. Treat it as if
*we* had just mined it — the historical nonce stands in for what the
solver "would have returned." The header_json's full_block_hex is the
block's canonical bytes verbatim (we have the body from getblock).

If the pipeline is correct, submitblock returns one of:
  - "duplicate"                — node already has this block
  - "duplicate-inconclusive"   — same content, different position in candidate set
  - "duplicate-invalid"        — Bitcoin Core has marked the block bad (e.g., on a
                                 stale fork). Means the bytes parsed; the node
                                 rejected it for chain-state reasons, not byte-form.

Any of those three responses *proves* our submitblock plumbing works end-to-end
on canonical bytes. Anything else ("bad-tx-mrklroot", "high-hash", "bad-prevblk"
without duplicate marker, parse errors) would indicate a reconstruction bug.

Run:
    python test_historical_submit.py
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from block_template import rpc  # noqa: E402

HERE = Path(__file__).resolve().parent
SUBMIT_BLOCK = HERE / "submit_block.py"


def bits_to_target(bits: int) -> int:
    exponent = bits >> 24
    mantissa = bits & 0x007fffff
    if exponent <= 3:
        return mantissa >> (8 * (3 - exponent))
    return mantissa << (8 * (exponent - 3))


def pick_test_block() -> dict:
    """Pick the median validated block in the pruned-available window."""
    chain = rpc("getblockchaininfo")
    tip = chain["blocks"]
    pruneheight = chain.get("pruneheight", 0)
    if tip - pruneheight < 5:
        sys.exit("no validated blocks available outside the pruning window — try later")
    target_height = (pruneheight + tip) // 2
    return {"height": target_height}


def fetch_block(height: int) -> dict:
    bh = rpc("getblockhash", [height])
    header_hex = rpc("getblockheader", [bh, False])
    info = rpc("getblockheader", [bh, True])
    raw_block_hex = rpc("getblock", [bh, 0])
    return {
        "hash": bh,
        "header_hex": header_hex,
        "info": info,
        "raw_block_hex": raw_block_hex,
    }


def build_header_json(blk: dict) -> dict:
    """Construct a header_json dict matching what block_template.py would emit
    for this block — but using its actual historical bytes."""
    info = blk["info"]
    header_bytes = bytes.fromhex(blk["header_hex"])
    bits_int = int.from_bytes(header_bytes[72:76], "little")
    target_hex = f"0x{bits_to_target(bits_int):064x}"
    return {
        "block_hash": blk["hash"],
        "block_height": info["height"],
        "raw_header_hex": blk["header_hex"],
        "raw_header_bytes": 80,
        "expected_double_sha256_display": blk["hash"],
        "fields": {
            "version": info["version"],
            "prev_block_hash_display": info.get("previousblockhash", "0" * 64),
            "merkle_root_display": info["merkleroot"],
            "timestamp": info["time"],
            "bits": f"0x{bits_int:08x}",
            "bits_int": bits_int,
            "nonce": info["nonce"],
            "target": target_hex,
        },
        "nonce_bit_range_1indexed": [609, 640],
        "full_block_hex": blk["raw_block_hex"],
    }


def make_solver_log(historical_nonce_bytes_BE: bytes) -> str:
    """Generate a synthetic 'v ...' line as if a solver had returned the
    historical nonce. submit_block.py only cares about variables 1..32."""
    if len(historical_nonce_bytes_BE) != 4:
        raise ValueError("nonce must be 4 bytes (network order)")
    units = []
    for byte_i, b in enumerate(historical_nonce_bytes_BE):
        for bit_in_byte in range(7, -1, -1):  # MSB first
            var = byte_i * 8 + (7 - bit_in_byte) + 1
            sign = 1 if (b >> bit_in_byte) & 1 else -1
            units.append(sign * var)
    return "s SATISFIABLE\nv " + " ".join(str(u) for u in units) + " 0\n"


def test_step_11_reconstruction(blk: dict) -> tuple[bool, str]:
    """submit_block.py's header reconstruction step (#11) should produce a
    full block whose bytes match the canonical historical block exactly."""
    header_json = build_header_json(blk)
    historical_nonce_BE = bytes.fromhex(blk["header_hex"])[76:80]
    solver_log_text = make_solver_log(historical_nonce_BE)

    tmpdir = Path(tempfile.mkdtemp(prefix="satcoin_hist_"))
    header_path = tmpdir / "header.json"
    log_path = tmpdir / "solver.log"
    header_path.write_text(json.dumps(header_json))
    log_path.write_text(solver_log_text)

    # Run submit_block.py WITHOUT --submit. It prints the assembled block
    # in its stdout/stderr stream.
    res = subprocess.run(
        [sys.executable, str(SUBMIT_BLOCK),
         "--header", str(header_path),
         "--solver-output", str(log_path)],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        return False, f"submit_block.py exited {res.returncode}: {res.stderr[:300]}"

    # Parse the JSON it printed (the verify summary).
    try:
        start = res.stdout.index("{"); end = res.stdout.rindex("}") + 1
        payload = json.loads(res.stdout[start:end])
    except (ValueError, json.JSONDecodeError) as e:
        return False, f"could not parse submit_block.py output: {e}"

    if not payload.get("meets_real_bitcoin_target"):
        return False, ("hashlib says hash doesn't meet target, but this is a "
                       "real historical block — encoding/parsing bug")
    if payload["reconstructed_header_hex"] != blk["header_hex"]:
        return False, ("reconstructed header doesn't match canonical:\n"
                       f"  got      {payload['reconstructed_header_hex']}\n"
                       f"  expected {blk['header_hex']}")
    return True, (f"reconstructed bytes == canonical header for block {blk['info']['height']}, "
                  f"hash {payload['double_sha256_displayed_hex'][:16]}...")


def test_step_12_submit(blk: dict) -> tuple[bool, str]:
    """Run submit_block.py with --submit. Expect a duplicate-* response from
    bitcoind (since the block is already in the chain). Anything else
    indicates either a reconstruction bug or an RPC plumbing bug."""
    header_json = build_header_json(blk)
    historical_nonce_BE = bytes.fromhex(blk["header_hex"])[76:80]
    solver_log_text = make_solver_log(historical_nonce_BE)

    tmpdir = Path(tempfile.mkdtemp(prefix="satcoin_hist_sub_"))
    header_path = tmpdir / "header.json"
    log_path = tmpdir / "solver.log"
    header_path.write_text(json.dumps(header_json))
    log_path.write_text(solver_log_text)

    res = subprocess.run(
        [sys.executable, str(SUBMIT_BLOCK),
         "--header", str(header_path),
         "--solver-output", str(log_path),
         "--submit"],
        capture_output=True, text=True,
    )
    output = (res.stdout or "") + (res.stderr or "")
    # The acceptance line we care about looks like:  "[submit] accepted: ..." or
    # "[submit] rejected: rejection reason: duplicate"
    expected_substrings = ("duplicate", "duplicate-inconclusive", "duplicate-invalid")
    found = next((s for s in expected_substrings if s in output), None)
    if found:
        return True, f"bitcoind responded '{found}' — bytes parsed correctly"
    # If accepted (impossible for an already-in-chain block), or some other error,
    # surface what happened.
    if "accepted" in output:
        return False, "node ACCEPTED the block (unexpected for a duplicate — bug?)"
    snippet = (output.replace("\n", " | ")[:400]) or "(no output)"
    return False, f"unexpected response. submit output: {snippet}"


def run_suite():
    print("\nhistorical end-to-end submit test (steps 11 + 12)")
    print("-" * 80)
    blk_meta = pick_test_block()
    print(f"selected height: {blk_meta['height']}")
    blk = fetch_block(blk_meta["height"])
    print(f"hash: {blk['hash']}")
    print(f"nonce (LE int): {blk['info']['nonce']:,}")
    print()

    all_ok = True
    for fn in (test_step_11_reconstruction, test_step_12_submit):
        try:
            ok, detail = fn(blk)
        except Exception as e:
            ok, detail = False, f"exception: {type(e).__name__}: {e}"
        marker = "PASS" if ok else "FAIL"
        if not ok:
            all_ok = False
        # nice name for the print: strip test_ prefix and underscores
        nice = fn.__name__.replace("test_", "").replace("_", " ")
        print(f"  {marker}: {nice} — {detail}")

    print("-" * 80)
    print("ALL PASS" if all_ok else "SOME FAILED")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(run_suite())
