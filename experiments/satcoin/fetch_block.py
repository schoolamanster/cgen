"""Fetch a Bitcoin block header for the satcoin experiment.

Two backends:
  - Default: blockstream.info public REST API (no install needed).
  - --rpc:  local Bitcoin Core JSON-RPC (much faster: ~10 ms vs ~1 s).
            Requires bitcoind running with `server=1`. Reads
            $APPDATA/Bitcoin/bitcoin.conf for rpcuser/rpcpassword.

Output: a JSON document with the raw 80-byte header plus parsed fields
and the difficulty target — same schema regardless of backend.

Usage:
    python fetch_block.py --height 0                       # blockstream API
    python fetch_block.py --hash <hash>                    # blockstream API
    python fetch_block.py --tip                            # blockstream API
    python fetch_block.py --height 0 --rpc                 # local node RPC
    python fetch_block.py --tip --rpc                      # local node RPC
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

API_BASE = "https://blockstream.info/api"
RPC_URL = "http://127.0.0.1:8332/"


def _rpc_creds() -> tuple[str, str]:
    """Read rpcuser/rpcpassword from bitcoin.conf in the standard data dir."""
    conf_path = os.path.join(os.environ.get("APPDATA", ""), "Bitcoin", "bitcoin.conf")
    if not os.path.exists(conf_path):
        sys.exit(f"--rpc requires {conf_path} with rpcuser/rpcpassword set")
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
    """Single JSON-RPC POST to the local node. Returns the result field."""
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
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        # Bitcoin Core puts JSON error details in the response body even on HTTP errors
        try:
            body = json.loads(e.read())
            sys.exit(f"RPC error {e.code}: {body.get('error', body)}")
        except Exception:
            sys.exit(f"RPC HTTP {e.code} on {method}: {e}")
    except urllib.error.URLError as e:
        sys.exit(f"RPC unreachable ({RPC_URL}) — is bitcoind running? {e.reason}")
    if body.get("error"):
        sys.exit(f"RPC {method} returned error: {body['error']}")
    return body["result"]


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
    p.add_argument("--rpc", action="store_true",
                   help="Use the local Bitcoin Core RPC instead of the public API. "
                        "Requires bitcoind running with server=1 and bitcoin.conf credentials.")
    args = p.parse_args()

    if args.rpc:
        # Local node path.
        if args.tip:
            bh = rpc_call("getbestblockhash", [])
        elif args.height is not None:
            bh = rpc_call("getblockhash", [args.height])
        else:
            bh = args.hash.lower()
        raw_hex = rpc_call("getblockheader", [bh, False])  # False = serialized hex
        info = rpc_call("getblockheader", [bh, True])      # True  = parsed object
        height = info["height"]
    else:
        # Public API path.
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
