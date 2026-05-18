"""Validate the block-construction logic against real historical blocks.

The construction tests in test_block_construction.py exercise primitives in
isolation. This file does the harder thing: pull a real validated block
from local RPC and confirm our pipeline's components reproduce its
on-the-wire bytes.

What we can verify against history (without getblocktemplate):
  - Merkle root: compute_merkle_root over the actual tx ids should match
    the merkle root committed in the actual header. This is the most
    bug-prone construction step and the hardest to get right; if our math
    is wrong, this test catches it immediately.
  - Coinbase txid: when we extract the historical coinbase tx bytes and
    run them through coinbase_txid(), the result must match the historical
    coinbase's known txid.
  - Header layout: when we re-pack the historical fields (version, prev
    hash, merkle root, time, bits, nonce) using our serialization order,
    we must reproduce the exact 80 bytes the node stores.

What we CAN'T verify offline:
  - assemble_block() end-to-end (no getblocktemplate yet). But if all the
    primitives match against history, end-to-end correctness reduces to
    field plumbing, which the offline tests cover.

Run:
    python test_historical_reconstruction.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from block_template import (  # noqa: E402
    rpc, double_sha256, compute_merkle_root, coinbase_txid,
)


def pick_test_blocks() -> list[dict]:
    """Choose blocks dynamically from the pruned node's currently-available range.

    Pruning may delete old blocks at any time, so we ask the node what's
    available right now and sample inside that window. Picking 3 heights
    near the tip gives good coverage and avoids racing the pruner.
    """
    chain = rpc("getblockchaininfo")
    tip = chain["blocks"]
    pruneheight = chain.get("pruneheight", 0)
    if tip - pruneheight < 10:
        return []  # nothing safely accessible
    # Sample three points well inside the available window. Stay 5 blocks
    # below tip (so anything still being validated doesn't race), and 5
    # above pruneheight (so pruning doesn't race us).
    lo = pruneheight + 5
    hi = tip - 5
    picks = [lo, (lo + hi) // 2, hi]
    return [{"label": f"block_{h}", "height": h} for h in picks]


HISTORICAL_BLOCKS = None  # set at runtime in run_suite()


def check(name: str, ok: bool, detail: str = "") -> tuple[bool, str]:
    return ok, f"  {'PASS' if ok else 'FAIL'}: {name}{(' — ' + detail) if detail else ''}"


def fetch_block_data(height: int) -> dict:
    bh = rpc("getblockhash", [height])
    block = rpc("getblock", [bh, 2])  # verbosity 2: full tx data
    header_hex = rpc("getblockheader", [bh, False])
    return {"hash": bh, "header_hex": header_hex, "block": block}


def test_merkle_root(b: dict) -> tuple[bool, str]:
    """Recompute the merkle root over actual tx ids; must match header."""
    block = b["block"]
    # tx ids in display order (big-endian); we need them little-endian for hashing.
    txid_bytes = [bytes.fromhex(tx["txid"])[::-1] for tx in block["tx"]]
    computed = compute_merkle_root(txid_bytes)
    expected_le = bytes.fromhex(block["merkleroot"])[::-1]
    ok = computed == expected_le
    detail = (f"computed={computed[::-1].hex()[:16]}... expected={block['merkleroot'][:16]}..."
              if not ok else
              f"{len(txid_bytes)} tx(s), root={block['merkleroot'][:16]}...")
    return check(f"merkle root (block {block['height']})", ok, detail)


def test_coinbase_txid(b: dict) -> tuple[bool, str]:
    """Extract the historical coinbase serialization and confirm our
    coinbase_txid() reproduces the known txid."""
    block = b["block"]
    coinbase = block["tx"][0]
    cb_hex = coinbase.get("hex")
    if not cb_hex:
        # Pull raw transaction separately if needed (some Core versions skip it
        # in getblock when txindex is off, but verbosity 2 should include it).
        cb_hex = rpc("getrawtransaction", [coinbase["txid"], False, b["hash"]])
    cb_bytes = bytes.fromhex(cb_hex)
    computed = coinbase_txid(cb_bytes)[::-1].hex()
    expected = coinbase["txid"]
    return check(
        f"coinbase txid (block {block['height']})",
        computed == expected,
        f"got {computed[:16]}... expected {expected[:16]}...",
    )


def test_header_serialization(b: dict) -> tuple[bool, str]:
    """Re-pack the historical header fields; must reproduce the on-the-wire bytes."""
    block = b["block"]
    # Construct the 80 bytes from parsed fields.
    version = block["version"]
    prev = bytes.fromhex(block["previousblockhash"])[::-1] if "previousblockhash" in block else b"\x00" * 32
    merkle = bytes.fromhex(block["merkleroot"])[::-1]
    time_ = block["time"]
    bits = int(block["bits"], 16)
    nonce = block["nonce"]
    header = (
        version.to_bytes(4, "little")
        + prev
        + merkle
        + time_.to_bytes(4, "little")
        + bits.to_bytes(4, "little")
        + nonce.to_bytes(4, "little")
    )
    expected = bytes.fromhex(b["header_hex"])
    ok = header == expected and len(header) == 80
    return check(
        f"header serialization (block {block['height']})",
        ok,
        f"header_hex matches getblockheader" if ok else
        f"got {header.hex()[:40]} expected {b['header_hex'][:40]}",
    )


def run_suite() -> int:
    print(f"\nhistorical-block reconstruction tests")
    print("-" * 80)
    blocks_to_test = pick_test_blocks()
    if not blocks_to_test:
        print("  SKIP: no validated blocks available (pruned node window empty)")
        print("-" * 80)
        return 0
    all_ok = True
    for entry in blocks_to_test:
        try:
            data = fetch_block_data(entry["height"])
        except RuntimeError as e:
            print(f"  SKIP: block {entry['height']} — {e}")
            continue
        for fn in (test_merkle_root, test_coinbase_txid, test_header_serialization):
            try:
                ok, line = fn(data)
            except Exception as e:
                ok, line = False, f"  FAIL: {fn.__name__} block {entry['height']} — exception {type(e).__name__}: {e}"
            print(line)
            if not ok:
                all_ok = False
    print("-" * 80)
    print("ALL PASS" if all_ok else "SOME FAILED")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(run_suite())
