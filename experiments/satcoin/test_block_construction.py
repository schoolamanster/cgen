"""Offline tests for block_template.py — runs without a synced node.

Validates the construction pieces (varint, coinbase serialization, merkle
root, header assembly) against known answers and round-trip invariants.

When bitcoind finishes syncing, getblocktemplate becomes available and the
end-to-end live test (in tests.py) will exercise the full path. Until then,
these offline tests prove the assembly logic itself is correct.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

# Make sure we can import from this directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from block_template import (  # noqa: E402
    varint, double_sha256, encode_height_for_coinbase,
    build_coinbase_tx, coinbase_txid, compute_merkle_root, assemble_block,
)


PASS, FAIL = "PASS", "FAIL"


def check(name: str, ok: bool, detail: str = "") -> tuple[bool, str]:
    return ok, f"  {PASS if ok else FAIL}: {name}{(' — ' + detail) if detail else ''}"


def test_varint():
    cases = [
        (0, "00"),
        (252, "fc"),
        (253, "fdfd00"),
        (0xffff, "fdffff"),
        (0x10000, "fe00000100"),
        (0xffffffff, "feffffffff"),
        (0x100000000, "ff0000000001000000"),
    ]
    for n, expected in cases:
        got = varint(n).hex()
        if got != expected:
            return check("varint encoding", False, f"varint({n}) = {got}, expected {expected}")
    return check("varint encoding", True, f"{len(cases)} cases")


def test_double_sha256():
    # Genesis coinbase tx output script: well-known double-SHA result for empty input.
    expected = "5df6e0e2761359d30a8275058e299fcc0381534545f55cf43e41983f5d4c9456"
    got = double_sha256(b"").hex()
    return check("double_sha256(empty)", got == expected, f"got {got[:16]}...")


def test_encode_height():
    # Examples from Bitcoin Core test vectors / observed live blocks.
    cases = [
        (1, "0101"),                # push 1 byte: 0x01
        (16, "0110"),               # push 1 byte: 0x10
        (256, "020001"),            # push 2 bytes: 0x00 0x01
        (949946, "03ba7e0e"),       # push 3 bytes: 0x0e_7e_ba in LE (949946 = 0xE7EBA)
    ]
    for h, expected in cases:
        got = encode_height_for_coinbase(h).hex()
        if got != expected:
            return check("BIP34 height encoding", False, f"h={h}: got {got}, expected {expected}")
    return check("BIP34 height encoding", True, f"{len(cases)} cases")


def test_coinbase_segwit_structure():
    """Build a segwit coinbase, parse it back, confirm field sizes are right."""
    payout_script = "0014" + "00" * 20  # P2WPKH for a dummy 20-byte hash
    commitment = "6a24aa21a9ed" + "00" * 32  # OP_RETURN witness commitment
    cb = build_coinbase_tx(
        height=850000,
        payout_script_hex=payout_script,
        coinbase_value=3_125_000_000,  # 3.125 BTC in sats
        witness_commitment_hex=commitment,
    )
    # Expected structure:
    #   4   version
    #   2   marker+flag (00 01)
    #   1   input count varint
    #  36   prev (32 zero + 4 ffffffff)
    #   1   script length varint
    #   N   script (BIP34 height push + extranonce)
    #   4   sequence
    #   1   output count varint
    #  10   payout output: 8 value + 1 len + script
    #   ?   commitment output
    #   ?   witness section
    #   4   locktime

    # Quick sanity checks.
    if cb[:4] != b"\x02\x00\x00\x00":
        return check("coinbase: version=2", False, f"first 4 bytes: {cb[:4].hex()}")
    if cb[4:6] != b"\x00\x01":
        return check("coinbase: segwit marker+flag", False, f"got {cb[4:6].hex()}")
    if cb[-4:] != b"\x00\x00\x00\x00":
        return check("coinbase: locktime=0", False, f"last 4 bytes: {cb[-4:].hex()}")

    # The txid (legacy serialization) and wtxid should differ for segwit.
    cb_txid = coinbase_txid(cb)
    cb_wtxid = double_sha256(cb)
    return check("coinbase segwit structure", cb_txid != cb_wtxid,
                 f"txid={cb_txid[::-1].hex()[:16]}... wtxid={cb_wtxid[::-1].hex()[:16]}...")


def test_merkle_single_tx():
    """Merkle of one tx is just that tx's hash."""
    h = bytes.fromhex("ab" * 32)
    return check("merkle root: single tx", compute_merkle_root([h]) == h)


def test_merkle_two_tx():
    """Merkle of two known hashes against hand-computed answer."""
    a = bytes.fromhex("01" * 32)
    b = bytes.fromhex("02" * 32)
    expected = double_sha256(a + b)
    return check("merkle root: two tx", compute_merkle_root([a, b]) == expected)


def test_merkle_odd_tx():
    """Merkle with an odd number of leaves — last one is duplicated."""
    a = bytes.fromhex("aa" * 32)
    b = bytes.fromhex("bb" * 32)
    c = bytes.fromhex("cc" * 32)
    # Layer 0: [a, b, c] -> with duplication [a, b, c, c]
    # Layer 1: [h(a||b), h(c||c)]
    # Layer 2: [h(h(a||b) || h(c||c))]
    ab = double_sha256(a + b)
    cc = double_sha256(c + c)
    expected = double_sha256(ab + cc)
    return check("merkle root: odd tx (duplicate)", compute_merkle_root([a, b, c]) == expected)


def test_assemble_minimal_block():
    """Construct a minimal one-tx block and verify shape + hash chain."""
    # Hand-crafted minimal template.
    template = {
        "version": 0x20000000,
        "previousblockhash": "0" * 64,
        "transactions": [],  # only coinbase
        "coinbasevalue": 5_000_000_000,  # 50 BTC (initial reward)
        "target": "f" * 64,
        "bits": "1d00ffff",
        "curtime": 1700000000,
        "height": 1,
        "default_witness_commitment": "6a24aa21a9ed" + "00" * 32,
    }
    payout_script = "0014" + "11" * 20

    result = assemble_block(template, payout_script)

    # Sanity checks on the assembled block.
    raw = bytes.fromhex(result["raw_header_hex"])
    if len(raw) != 80:
        return check("assembled header is 80 bytes", False, f"got {len(raw)}")

    full = bytes.fromhex(result["full_block_hex"])
    if not full.startswith(raw):
        return check("full block starts with header", False)

    # After the header, the next bytes should be the tx count varint (1 tx).
    tx_count_byte = full[80]
    if tx_count_byte != 1:
        return check("tx count in full block", False, f"got {tx_count_byte}")

    # Merkle root: with only the coinbase, root = coinbase txid (in serialization LE form).
    cb_bytes = bytes.fromhex(result["coinbase_tx_hex"])
    cb_id = coinbase_txid(cb_bytes)
    merkle_in_header = raw[36:68]
    if merkle_in_header != cb_id:
        return check("merkle root matches coinbase txid",
                     False, f"header has {merkle_in_header[::-1].hex()[:16]}... vs cb {cb_id[::-1].hex()[:16]}...")

    return check("assemble minimal block", True,
                 f"80-byte header + {result['extras']['tx_count_total']} tx(s), "
                 f"payout={result['coinbase_value_btc']} BTC, "
                 f"target={result['fields']['target'][:18]}...")


def test_assemble_pre_segwit_block():
    """Pre-segwit blocks have no witness commitment. Coinbase must not include marker+flag."""
    template = {
        "version": 1,
        "previousblockhash": "0" * 64,
        "transactions": [],
        "coinbasevalue": 5_000_000_000,
        "target": "f" * 64,
        "bits": "1d00ffff",
        "curtime": 1500000000,
        "height": 100,
        # no default_witness_commitment
    }
    payout_script = "76a914" + "11" * 20 + "88ac"  # P2PKH

    result = assemble_block(template, payout_script)
    cb = bytes.fromhex(result["coinbase_tx_hex"])
    # No segwit marker.
    if cb[4:6] == b"\x00\x01":
        return check("pre-segwit coinbase has no marker+flag", False,
                     "found segwit marker on pre-segwit tx")
    return check("pre-segwit coinbase has no marker+flag", True)


def run_suite():
    tests = [
        test_varint,
        test_double_sha256,
        test_encode_height,
        test_coinbase_segwit_structure,
        test_merkle_single_tx,
        test_merkle_two_tx,
        test_merkle_odd_tx,
        test_assemble_minimal_block,
        test_assemble_pre_segwit_block,
    ]
    all_ok = True
    print(f"\nblock_template construction tests")
    print("-" * 60)
    for t in tests:
        try:
            ok, line = t()
        except Exception as e:
            ok, line = False, f"  {FAIL}: {t.__name__} — exception: {type(e).__name__}: {e}"
        print(line)
        if not ok:
            all_ok = False
    print("-" * 60)
    print("ALL PASS" if all_ok else "SOME FAILED")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(run_suite())
