"""Regression tests for the nonce_pinning helper.

These exist specifically to prevent the test-only byte-order class of bug
that came up during a live-block verification: it's tempting to write
`(displayed_nonce_int).to_bytes(4, 'big')` and that produces the wrong
bytes for cgen's variable layout.

Each test uses a known historical block as a test vector. If any of these
fail in a future refactor, the helper has regressed; fix it before using
the pipeline against live data.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nonce_pinning import (  # noqa: E402
    nonce_bytes_from_header, unit_clauses_for_nonce,
    nonce_int_from_assignment,
)


def check(name: str, ok: bool, detail: str = "") -> tuple[bool, str]:
    return ok, f"  {'PASS' if ok else 'FAIL'}: {name}{(' — ' + detail) if detail else ''}"


# Test vector: block 950,416 (verified live, May 21 2026).
#   displayed nonce: 150,978,030 = 0x08ffbdee
#   header bytes 76..79 (on the wire): eebdff08
BLOCK_950416_HEADER = (
    "0000022061ed47f14fe7b7fec0d92e14bf789a5e4857cdebb85d0100000000000000000053"
    "c5d91546a23a2c8f5620507da266032d9e34cf9ba87695f25a3bd0b2192b2446760f6a790f"
    "0217eebdff08"
)
BLOCK_950416_NONCE_DISPLAYED = 150_978_030
BLOCK_950416_NONCE_BYTES_NETWORK = "eebdff08"


def test_nonce_bytes_extracted_from_network_order():
    """The helper must return the network-order bytes, NOT the LE int's BE serialization."""
    bs = nonce_bytes_from_header(BLOCK_950416_HEADER)
    ok = bs.hex() == BLOCK_950416_NONCE_BYTES_NETWORK
    return check("nonce_bytes_from_header returns network-order bytes",
                 ok,
                 f"got {bs.hex()}, expected {BLOCK_950416_NONCE_BYTES_NETWORK}")


def test_helper_disagrees_with_naive_int_to_bytes_big():
    """A common test-side mistake is `displayed_nonce.to_bytes(4, 'big')`.
    The helper must produce different bytes than that wrong approach."""
    naive_wrong = BLOCK_950416_NONCE_DISPLAYED.to_bytes(4, "big").hex()
    correct = nonce_bytes_from_header(BLOCK_950416_HEADER).hex()
    return check("helper does NOT match the naive int.to_bytes(4,'big') mistake",
                 naive_wrong != correct,
                 f"naive wrong={naive_wrong}, correct={correct}")


def test_unit_clauses_first_byte():
    """First 8 unit clauses must encode byte 0xee = 1110_1110 (MSB-first)."""
    bs = bytes.fromhex(BLOCK_950416_NONCE_BYTES_NETWORK)
    units = unit_clauses_for_nonce(bs)
    expected = [1, 2, 3, -4, 5, 6, 7, -8]
    return check("first byte (0xee) -> unit clauses [+1 +2 +3 -4 +5 +6 +7 -8]",
                 units[:8] == expected,
                 f"got {units[:8]}")


def test_unit_clauses_all_ones_byte():
    """Byte 0xff = 1111_1111 should produce all-positive units for that range."""
    bs = bytes.fromhex(BLOCK_950416_NONCE_BYTES_NETWORK)
    units = unit_clauses_for_nonce(bs)
    expected = list(range(17, 25))  # vars 17..24 all positive
    return check("byte 0xff -> all positive (+17..+24)",
                 units[16:24] == expected,
                 f"got {units[16:24]}")


def test_unit_clauses_sparse_byte():
    """Byte 0x08 = 0000_1000 — only bit 3 set (var 29 positive)."""
    bs = bytes.fromhex(BLOCK_950416_NONCE_BYTES_NETWORK)
    units = unit_clauses_for_nonce(bs)
    expected = [-25, -26, -27, -28, 29, -30, -31, -32]
    return check("byte 0x08 -> [-25 -26 -27 -28 +29 -30 -31 -32]",
                 units[24:32] == expected,
                 f"got {units[24:32]}")


def test_roundtrip_assignment_to_nonce():
    """If you set up an assignment matching the unit clauses, the recovered
    displayed-int nonce must equal the original displayed int."""
    bs = bytes.fromhex(BLOCK_950416_NONCE_BYTES_NETWORK)
    units = unit_clauses_for_nonce(bs)
    assignment = {abs(u): u > 0 for u in units}
    recovered = nonce_int_from_assignment(assignment)
    return check("assignment -> displayed-int nonce round-trips",
                 recovered == BLOCK_950416_NONCE_DISPLAYED,
                 f"got {recovered:,}, expected {BLOCK_950416_NONCE_DISPLAYED:,}")


def test_short_input_rejected():
    """nonce_bytes_from_header must raise on wrong-length input."""
    try:
        nonce_bytes_from_header("ab" * 79)  # 158 chars, not 160
        return check("short header is rejected", False, "no exception raised")
    except ValueError:
        return check("short header is rejected", True)


def test_unit_clauses_wrong_length_rejected():
    """unit_clauses_for_nonce must raise on != 4 bytes."""
    try:
        unit_clauses_for_nonce(b"\x00\x00\x00")
        return check("non-4-byte nonce is rejected", False, "no exception raised")
    except ValueError:
        return check("non-4-byte nonce is rejected", True)


def run_suite() -> int:
    tests = [
        test_nonce_bytes_extracted_from_network_order,
        test_helper_disagrees_with_naive_int_to_bytes_big,
        test_unit_clauses_first_byte,
        test_unit_clauses_all_ones_byte,
        test_unit_clauses_sparse_byte,
        test_roundtrip_assignment_to_nonce,
        test_short_input_rejected,
        test_unit_clauses_wrong_length_rejected,
    ]
    print("\nnonce_pinning helper tests")
    print("-" * 70)
    all_ok = True
    for t in tests:
        try:
            ok, line = t()
        except Exception as e:
            ok = False
            line = f"  FAIL: {t.__name__} — exception {type(e).__name__}: {e}"
        print(line)
        if not ok:
            all_ok = False
    print("-" * 70)
    print("ALL PASS" if all_ok else "SOME FAILED")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(run_suite())
