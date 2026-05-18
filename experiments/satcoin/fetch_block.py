"""Fetch a Bitcoin block header for the satcoin experiment.

Pulls an 80-byte block header from blockstream.info's public REST API and
prints a JSON document with the raw bytes plus everything downstream tools
need: parsed fields, the difficulty target, and the expected double-SHA-256
(so other steps can verify their work).

No external dependencies. Single HTTP call per fetch (two for --height,
since we resolve height to hash first). No auth, no rate-limit issues at
the volumes this experiment generates.

Usage:
    python fetch_block.py --height 0
    python fetch_block.py --hash 000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f
    python fetch_block.py --tip
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request

API_BASE = "https://blockstream.info/api"


def _http_get(url: str) -> str:
    """Tiny stdlib HTTP GET. Returns the response body as a string.

    blockstream.info returns text/plain for the endpoints we use, so no
    JSON parsing is needed here.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "satcoin-experiment/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode("ascii").strip()
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code} from {url}: {e.read().decode('utf-8', 'replace')}")
    except urllib.error.URLError as e:
        sys.exit(f"Network error fetching {url}: {e.reason}")


def height_to_hash(height: int) -> str:
    return _http_get(f"{API_BASE}/block-height/{height}")


def tip_hash() -> str:
    return _http_get(f"{API_BASE}/blocks/tip/hash")


def header_hex(block_hash: str) -> str:
    """Returns the 160-hex-char (80-byte) block header for the given hash."""
    h = _http_get(f"{API_BASE}/block/{block_hash}/header")
    if len(h) != 160:
        sys.exit(f"Expected 160 hex chars, got {len(h)}: {h!r}")
    return h


def block_height(block_hash: str) -> int:
    """Returns the block's height from the metadata endpoint."""
    raw = _http_get(f"{API_BASE}/block/{block_hash}")
    # /block/<hash> returns JSON; the height field is what we want.
    info = json.loads(raw)
    return info["height"]


def double_sha256(data: bytes) -> bytes:
    """Bitcoin's hash: SHA-256 applied twice."""
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def bits_to_target(bits: int) -> int:
    """Decode the compact 'bits' difficulty representation into a 256-bit target.

    The 'bits' field is a 32-bit packed encoding: the high byte is an exponent,
    the low 3 bytes are a mantissa. target = mantissa << (8 * (exponent - 3)).
    """
    exponent = bits >> 24
    mantissa = bits & 0x007fffff
    if exponent <= 3:
        return mantissa >> (8 * (3 - exponent))
    return mantissa << (8 * (exponent - 3))


def parse_header(header_bytes: bytes) -> dict:
    """Decode the 80-byte header into its named fields.

    All multi-byte integer fields in the header are little-endian on the wire.
    The two 32-byte hash fields (prev block, merkle root) are stored little-
    endian byte-reversed relative to how they're displayed; we present both
    forms so downstream code can pick.
    """
    if len(header_bytes) != 80:
        raise ValueError(f"Expected 80 bytes, got {len(header_bytes)}")

    version = int.from_bytes(header_bytes[0:4], "little")
    prev_hash_le = header_bytes[4:36]
    merkle_le = header_bytes[36:68]
    timestamp = int.from_bytes(header_bytes[68:72], "little")
    bits = int.from_bytes(header_bytes[72:76], "little")
    nonce = int.from_bytes(header_bytes[76:80], "little")

    return {
        "version": version,
        "prev_block_hash_display": prev_hash_le[::-1].hex(),  # how block explorers show it
        "prev_block_hash_internal": prev_hash_le.hex(),       # how it's hashed
        "merkle_root_display": merkle_le[::-1].hex(),
        "merkle_root_internal": merkle_le.hex(),
        "timestamp": timestamp,
        "bits": f"0x{bits:08x}",
        "bits_int": bits,
        "nonce": nonce,
        "target": f"0x{bits_to_target(bits):064x}",
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--height", type=int, help="Fetch the block at this height.")
    g.add_argument("--hash", type=str, help="Fetch the block with this hash.")
    g.add_argument("--tip", action="store_true", help="Fetch the current chain tip.")
    args = p.parse_args()

    if args.height is not None:
        bh = height_to_hash(args.height)
        height = args.height
    elif args.hash:
        bh = args.hash.lower()
        height = block_height(bh)
    else:
        bh = tip_hash()
        height = block_height(bh)

    raw_hex = header_hex(bh)
    raw_bytes = bytes.fromhex(raw_hex)

    # The displayed block hash IS the double-SHA-256 of the header,
    # byte-reversed. Verifying this here proves we have the right header
    # before downstream tools waste cycles encoding it.
    computed = double_sha256(raw_bytes)[::-1].hex()
    if computed != bh.lower():
        sys.exit(
            f"Hash mismatch: API hash={bh}, computed double-SHA256={computed}.\n"
            "Either the API is wrong or our parsing is. Stopping."
        )

    parsed = parse_header(raw_bytes)
    output = {
        "block_hash": bh,
        "block_height": height,
        "raw_header_hex": raw_hex,
        "raw_header_bytes": 80,
        "expected_double_sha256_display": bh,
        "fields": parsed,
        # The 4-byte nonce occupies bit positions 609..640 (1-indexed)
        # of the 640-bit header, which becomes the cgen -vM input.
        # build_satcoin_cnf.py uses this to tell cgen which bits stay free.
        "nonce_bit_range_1indexed": [609, 640],
    }
    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
