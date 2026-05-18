"""Construct a full Bitcoin candidate block from a getblocktemplate response.

The 80-byte header is what gets hashed (and what cgen encodes). But to
*submit* a block, the network needs the full block: header + coinbase
transaction (paying us) + every other transaction matching the merkle
root committed in the header.

This module:
  1. Calls getblocktemplate via local RPC (needs bitcoind synced to tip)
  2. Resolves our payout address into its scriptPubKey
  3. Constructs a BIP34-compliant, segwit-aware coinbase transaction
  4. Computes the merkle root over (coinbase || other transactions)
  5. Assembles the 80-byte header
  6. Serializes the full block hex (ready for submitblock)

The output schema matches fetch_block.py's so build_satcoin_cnf.py can
consume it identically — *plus* a `full_block_hex` field that
submit_block.py uses when a solution is found.

When sync isn't finished, getblocktemplate fails. The block_from_template()
function works on saved-template JSON so we can unit-test construction
without a synced node.

Usage:
    python block_template.py                    # live: call getblocktemplate
    python block_template.py --save tpl.json    # save raw template
    python block_template.py --load tpl.json    # assemble from saved template
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

RPC_URL = "http://127.0.0.1:8332/"
WALLET = "satcoin"
PAYOUT_LABEL = "mining_payout"


# ---------------------------------------------------------------------------
# RPC plumbing (same shape as fetch_block.py/submit_block.py)
# ---------------------------------------------------------------------------

def _rpc_creds() -> tuple[str, str]:
    conf_path = os.path.join(os.environ.get("APPDATA", ""), "Bitcoin", "bitcoin.conf")
    user = pw = None
    with open(conf_path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("rpcuser="):
                user = line.split("=", 1)[1]
            elif line.startswith("rpcpassword="):
                pw = line.split("=", 1)[1]
    return user, pw


def rpc(method: str, params: list | None = None, wallet: str | None = None):
    user, pw = _rpc_creds()
    auth = base64.b64encode(f"{user}:{pw}".encode()).decode()
    url = f"{RPC_URL}wallet/{wallet}" if wallet else RPC_URL
    payload = json.dumps({"jsonrpc": "1.0", "id": "tpl", "method": method, "params": params or []}).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
            raise RuntimeError(f"RPC {method} HTTP {e.code}: {body.get('error', body)}")
        except json.JSONDecodeError:
            raise RuntimeError(f"RPC {method} HTTP {e.code}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"bitcoind unreachable: {e.reason}")
    if body.get("error"):
        raise RuntimeError(f"RPC {method} error: {body['error']}")
    return body["result"]


# ---------------------------------------------------------------------------
# Byte serialization primitives — Bitcoin's funky encoding
# ---------------------------------------------------------------------------

def varint(n: int) -> bytes:
    """Bitcoin's compact size encoding. Small numbers are short; big ones get a prefix byte."""
    if n < 0xfd:
        return n.to_bytes(1, "little")
    if n <= 0xffff:
        return b"\xfd" + n.to_bytes(2, "little")
    if n <= 0xffffffff:
        return b"\xfe" + n.to_bytes(4, "little")
    return b"\xff" + n.to_bytes(8, "little")


def double_sha256(b: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


def encode_height_for_coinbase(height: int) -> bytes:
    """BIP34: coinbase script must start with the block's height as a script-encoded number.

    CScriptNum-style: minimal-LE bytes with a sign bit on the last byte if needed.
    For all real heights (small positive int), this is just LE bytes prefixed by a
    push-N opcode.
    """
    if height == 0:
        return b"\x00"  # OP_0
    bs = bytearray()
    n = height
    neg = n < 0
    if neg:
        n = -n
    while n:
        bs.append(n & 0xff)
        n >>= 8
    # If the most significant bit is set, we need an extra byte so the
    # interpreter doesn't treat it as negative.
    if bs[-1] & 0x80:
        bs.append(0x80 if neg else 0x00)
    elif neg:
        bs[-1] |= 0x80
    return bytes([len(bs)]) + bytes(bs)


# ---------------------------------------------------------------------------
# Coinbase transaction
# ---------------------------------------------------------------------------

def build_coinbase_tx(*, height: int, payout_script_hex: str,
                     coinbase_value: int, witness_commitment_hex: str | None,
                     extranonce: bytes = b"\x00\x00\x00\x00") -> bytes:
    """Construct a segwit-aware BIP34 coinbase transaction.

    Args:
      height: block height (encoded into coinbase script per BIP34).
      payout_script_hex: scriptPubKey of the payout address (from getaddressinfo).
      coinbase_value: block reward + fees, in satoshis.
      witness_commitment_hex: from `default_witness_commitment` in the template, or None
                              if this is a pre-segwit block (height < 481824 on mainnet).
                              When present, an OP_RETURN output carrying the commitment
                              is added. Marker+flag and a witness field are also added.
      extranonce: arbitrary bytes appended after the BIP34 height in the coinbase script.
                  Real miners iterate this when they exhaust the nonce field. We default
                  to 4 zero bytes; nothing in our pipeline currently varies it.

    Returns the serialized transaction bytes.
    """
    is_segwit = witness_commitment_hex is not None

    # Inputs: a single coinbase input.
    coinbase_script = encode_height_for_coinbase(height) + extranonce
    inp = (b"\x00" * 32) + (0xffffffff).to_bytes(4, "little") \
        + varint(len(coinbase_script)) + coinbase_script \
        + (0xffffffff).to_bytes(4, "little")

    # Outputs.
    payout_script = bytes.fromhex(payout_script_hex)
    out_payout = coinbase_value.to_bytes(8, "little") \
        + varint(len(payout_script)) + payout_script

    outs = [out_payout]
    if is_segwit:
        # Witness commitment goes in an OP_RETURN output of value 0.
        commitment_script = bytes.fromhex(witness_commitment_hex)
        out_commitment = (0).to_bytes(8, "little") \
            + varint(len(commitment_script)) + commitment_script
        outs.append(out_commitment)

    # Assemble.
    tx = bytearray()
    tx += (2).to_bytes(4, "little")  # version 2 (required for BIP34)
    if is_segwit:
        tx += b"\x00\x01"            # segwit marker + flag
    tx += varint(1)                  # one input
    tx += inp
    tx += varint(len(outs))
    for o in outs:
        tx += o
    if is_segwit:
        # Witness for input 0: one stack item of 32 zero bytes (the witness reserved value).
        tx += varint(1)              # one witness item
        tx += varint(32) + (b"\x00" * 32)
    tx += (0).to_bytes(4, "little")  # locktime
    return bytes(tx)


def coinbase_txid(coinbase_tx_bytes: bytes) -> bytes:
    """Compute the txid of a coinbase tx — the NON-WITNESS double-SHA-256.

    For segwit transactions, the txid is over the legacy serialization (no
    marker, flag, or witness data). We strip those out here.
    """
    if len(coinbase_tx_bytes) < 6:
        raise ValueError("tx too short")
    # Check for segwit marker.
    if coinbase_tx_bytes[4] == 0 and coinbase_tx_bytes[5] == 1:
        # Strip the marker+flag and the witness section.
        # The witness section is at the end, before the locktime (last 4 bytes).
        # Parse forward to find where the outputs end, then the witness starts there.
        legacy = bytearray()
        legacy += coinbase_tx_bytes[:4]            # version
        # Skip bytes 4 and 5 (marker, flag).
        cursor = 6
        # Inputs.
        n_in = coinbase_tx_bytes[cursor]; cursor += 1
        legacy.append(n_in)
        for _ in range(n_in):
            # prev hash + prev index (32 + 4 bytes)
            legacy += coinbase_tx_bytes[cursor:cursor+36]; cursor += 36
            # script length + script
            slen = coinbase_tx_bytes[cursor]; legacy.append(slen); cursor += 1
            legacy += coinbase_tx_bytes[cursor:cursor+slen]; cursor += slen
            # sequence
            legacy += coinbase_tx_bytes[cursor:cursor+4]; cursor += 4
        # Outputs.
        n_out = coinbase_tx_bytes[cursor]; cursor += 1
        legacy.append(n_out)
        for _ in range(n_out):
            legacy += coinbase_tx_bytes[cursor:cursor+8]; cursor += 8
            slen = coinbase_tx_bytes[cursor]; legacy.append(slen); cursor += 1
            legacy += coinbase_tx_bytes[cursor:cursor+slen]; cursor += slen
        # Witness section starts here. We skip it and grab the locktime (last 4 bytes).
        legacy += coinbase_tx_bytes[-4:]
        return double_sha256(bytes(legacy))
    return double_sha256(coinbase_tx_bytes)


# ---------------------------------------------------------------------------
# Merkle root
# ---------------------------------------------------------------------------

def compute_merkle_root(txid_bytes_list: list[bytes]) -> bytes:
    """Standard Bitcoin merkle. txid_bytes_list is in serialization (LE) form.

    Each level: pair up adjacent hashes (duplicate the last if count is odd),
    concatenate, double-SHA-256. Continue until one hash remains.
    """
    layer = list(txid_bytes_list)
    if not layer:
        raise ValueError("empty tx list")
    while len(layer) > 1:
        if len(layer) % 2 == 1:
            layer.append(layer[-1])
        nxt = []
        for i in range(0, len(layer), 2):
            nxt.append(double_sha256(layer[i] + layer[i + 1]))
        layer = nxt
    return layer[0]


# ---------------------------------------------------------------------------
# Full block assembly
# ---------------------------------------------------------------------------

def assemble_block(template: dict, payout_script_hex: str) -> dict:
    """Given a getblocktemplate result and a payout scriptPubKey, return:
        {
          "header_json": same shape as fetch_block.py output (with parsed fields),
          "full_block_hex": ready for submitblock,
          "coinbase_value_btc": for human readability,
        }
    """
    height = template["height"]
    coinbase_value = template["coinbasevalue"]  # satoshis
    witness_commitment = template.get("default_witness_commitment")  # may be absent pre-segwit

    # 1. Build the coinbase transaction.
    coinbase_bytes = build_coinbase_tx(
        height=height,
        payout_script_hex=payout_script_hex,
        coinbase_value=coinbase_value,
        witness_commitment_hex=witness_commitment,
    )
    cb_txid = coinbase_txid(coinbase_bytes)

    # 2. Collect other transaction ids. getblocktemplate gives them as the
    #    big-endian display hex; we need them as little-endian bytes.
    other_txid_bytes = []
    other_tx_hexes = []
    for t in template["transactions"]:
        # txid is the non-witness hash (legacy). For non-segwit txs it's the same as hash;
        # for segwit, it's different. We use the txid for merkle.
        txid_le = bytes.fromhex(t["txid"])[::-1]
        other_txid_bytes.append(txid_le)
        other_tx_hexes.append(t["data"])

    # 3. Merkle root = double-SHA over (coinbase || others) tx ids in LE.
    merkle_root_le = compute_merkle_root([cb_txid] + other_txid_bytes)

    # 4. Build the 80-byte header.
    version = template["version"]
    prev_hash_le = bytes.fromhex(template["previousblockhash"])[::-1]
    timestamp = template["curtime"]
    bits = int(template["bits"], 16)
    nonce = 0  # placeholder; will be overwritten if mining succeeds

    header = bytearray()
    header += version.to_bytes(4, "little")
    header += prev_hash_le                            # 32 bytes
    header += merkle_root_le                          # 32 bytes
    header += timestamp.to_bytes(4, "little")
    header += bits.to_bytes(4, "little")
    header += nonce.to_bytes(4, "little")
    assert len(header) == 80

    # 5. Serialize full block: header + tx count (varint) + serialized txs.
    body = bytearray()
    body += varint(1 + len(template["transactions"]))
    body += coinbase_bytes
    for tx_hex in other_tx_hexes:
        body += bytes.fromhex(tx_hex)
    full_block = bytes(header) + bytes(body)

    # 6. Build the header JSON our existing tools expect.
    # Compute the predicted block hash for the placeholder nonce (just for verification trace).
    placeholder_hash = double_sha256(bytes(header))[::-1].hex()
    target_int = (int(template["bits"], 16) & 0x007fffff) << (8 * ((int(template["bits"], 16) >> 24) - 3))
    target_hex = f"0x{target_int:064x}"

    header_json = {
        "block_hash": placeholder_hash,  # WILL change once nonce is mined
        "block_height": height,
        "raw_header_hex": header.hex(),
        "raw_header_bytes": 80,
        "expected_double_sha256_display": placeholder_hash,  # placeholder
        "fields": {
            "version": version,
            "prev_block_hash_display": prev_hash_le[::-1].hex(),
            "prev_block_hash_internal": prev_hash_le.hex(),
            "merkle_root_display": merkle_root_le[::-1].hex(),
            "merkle_root_internal": merkle_root_le.hex(),
            "timestamp": timestamp,
            "bits": template["bits"],
            "bits_int": int(template["bits"], 16),
            "nonce": nonce,
            "target": target_hex,
        },
        "nonce_bit_range_1indexed": [609, 640],
        "coinbase_tx_hex": coinbase_bytes.hex(),
        "full_block_hex": full_block.hex(),
        "coinbase_value_sats": coinbase_value,
        "coinbase_value_btc": coinbase_value / 1e8,
        "payout_script_hex": payout_script_hex,
        "is_segwit": witness_commitment is not None,
        "extras": {
            "tx_count_total": 1 + len(template["transactions"]),
            "template_source": "getblocktemplate",
        },
    }
    return header_json


# ---------------------------------------------------------------------------
# Live fetch
# ---------------------------------------------------------------------------

def get_template_and_payout() -> tuple[dict, str]:
    """Call getblocktemplate and resolve our payout address into a scriptPubKey.

    Raises if bitcoind isn't synced or the wallet doesn't exist.
    """
    template = rpc("getblocktemplate", [{"rules": ["segwit"]}])
    # Get our payout address from the wallet (or use a saved one).
    addrs = rpc("getaddressesbylabel", [PAYOUT_LABEL], wallet=WALLET)
    if not addrs:
        addr = rpc("getnewaddress", [PAYOUT_LABEL, "bech32"], wallet=WALLET)
    else:
        addr = next(iter(addrs))
    info = rpc("getaddressinfo", [addr], wallet=WALLET)
    script_hex = info["scriptPubKey"]
    return template, script_hex


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = p.add_mutually_exclusive_group()
    g.add_argument("--save", type=str, help="Fetch live template and save raw to file (does not assemble)")
    g.add_argument("--load", type=str, help="Load saved template JSON and assemble block from it")
    args = p.parse_args()

    if args.save:
        try:
            template, _ = get_template_and_payout()
        except RuntimeError as e:
            sys.exit(f"Could not fetch live template: {e}")
        with open(args.save, "w") as f:
            json.dump(template, f, indent=2)
        print(f"Saved live template to {args.save}", file=sys.stderr)
        return

    if args.load:
        with open(args.load) as f:
            template = json.load(f)
        # We still need a payout script for assembly. Try via RPC; if unreachable,
        # use the default mining_payout address from the wallet OR exit cleanly.
        try:
            _, script_hex = get_template_and_payout()
        except RuntimeError as e:
            sys.exit(f"Need wallet RPC available to resolve payout script: {e}")
        header_json = assemble_block(template, script_hex)
        print(json.dumps(header_json, indent=2))
        return

    # Default: live fetch + assemble.
    try:
        template, script_hex = get_template_and_payout()
    except RuntimeError as e:
        sys.exit(
            f"Could not fetch live template: {e}\n"
            f"This typically means bitcoind isn't synced to chain tip yet. "
            f"getblocktemplate requires full validation. Use --save with a "
            f"future synced run, or test offline with --load on a saved template."
        )
    header_json = assemble_block(template, script_hex)
    print(json.dumps(header_json, indent=2))


if __name__ == "__main__":
    main()
