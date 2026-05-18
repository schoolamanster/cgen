"""Inspect the local Bitcoin Core wallet used by this experiment.

Shows balance, addresses, and recent transactions. Read-only — no sends.

Usage:
    python wallet.py                  # summary
    python wallet.py --addresses      # list known addresses
    python wallet.py --balance        # just the BTC number, machine-readable
    python wallet.py --new-address    # generate a fresh payout address
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

RPC_URL = "http://127.0.0.1:8332/"
WALLET = "satcoin"
CLI = r"C:\Program Files\Bitcoin\daemon\bitcoin-cli.exe"


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


def rpc(method: str, params: list | None = None, wallet: str | None = WALLET):
    """JSON-RPC call. If `wallet` is set, scoped to that wallet endpoint."""
    user, pw = _rpc_creds()
    auth = base64.b64encode(f"{user}:{pw}".encode()).decode()
    url = f"{RPC_URL}wallet/{wallet}" if wallet else RPC_URL
    payload = json.dumps({"jsonrpc": "1.0", "id": "wallet", "method": method, "params": params or []}).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except Exception:
            sys.exit(f"RPC HTTP {e.code}: {e}")
    except urllib.error.URLError as e:
        sys.exit(f"bitcoind unreachable: {e.reason}")
    if body.get("error"):
        sys.exit(f"RPC {method} error: {body['error']}")
    return body["result"]


def summary():
    info = rpc("getwalletinfo")
    # Bitcoin Core 31 changed wallet info shape — balance is now nested.
    bal = info.get("balance")
    if isinstance(bal, dict):
        # New shape: { "trusted": x, "untrusted_pending": y, "immature": z }
        trusted = bal.get("trusted", 0)
        pending = bal.get("untrusted_pending", 0)
        immature = bal.get("immature", 0)
    else:
        # Old shape: balance is a number; other fields top-level
        trusted = info.get("balance", 0)
        pending = info.get("unconfirmed_balance", 0)
        immature = info.get("immature_balance", 0)

    chain = rpc("getblockchaininfo", wallet=None)

    print(f"Wallet:           {info.get('walletname', '?')}")
    print(f"Balance (confirmed):    {trusted:.8f} BTC")
    print(f"Balance (unconfirmed):  {pending:.8f} BTC")
    print(f"Balance (immature):     {immature:.8f} BTC  (coinbase needs 100 confirmations)")
    print(f"TX count:         {info.get('txcount', 0)}")
    print(f"Keypool size:     {info.get('keypoolsize', '?')}")
    print()
    print(f"Node sync:        {chain['blocks']:,} / {chain['headers']:,} blocks "
          f"({chain['blocks']/chain['headers']*100:.2f}% validated)")
    if chain.get('pruned'):
        print(f"Mode:             pruned ({chain.get('prune_target_size', 0)/(1024**3):.1f} GB target, "
              f"{chain.get('size_on_disk', 0)/(1024**2):.0f} MB on disk)")


def addresses():
    # Method 1: walletcreatefundedpsbt won't help; use listreceivedbyaddress.
    rec = rpc("listreceivedbyaddress", [0, True, True])
    if not rec:
        print("(no addresses with received transactions; use --new-address to create one)")
    for entry in rec:
        addr = entry.get("address", "?")
        amount = entry.get("amount", 0)
        label = entry.get("label", "")
        confs = entry.get("confirmations", 0)
        print(f"  {addr}  {amount:>10.8f} BTC  conf={confs}  [{label}]")
    # Also try getaddressesbylabel for labels we know we used
    print("\nAddresses by label 'mining_payout':")
    try:
        labels = rpc("getaddressesbylabel", ["mining_payout"])
        for addr in labels:
            print(f"  {addr}")
    except SystemExit:
        print("  (none)")


def new_address(label: str = "mining_payout"):
    addr = rpc("getnewaddress", [label, "bech32"])
    print(addr)


def balance_only():
    info = rpc("getwalletinfo")
    bal = info.get("balance")
    if isinstance(bal, dict):
        print(f"{bal.get('trusted', 0):.8f}")
    else:
        print(f"{info.get('balance', 0):.8f}")


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = p.add_mutually_exclusive_group()
    g.add_argument("--addresses", action="store_true", help="List known addresses.")
    g.add_argument("--balance", action="store_true", help="Print balance only (BTC).")
    g.add_argument("--new-address", action="store_true", help="Generate a fresh payout address.")
    args = p.parse_args()

    if args.addresses:
        addresses()
    elif args.balance:
        balance_only()
    elif args.new_address:
        new_address()
    else:
        summary()


if __name__ == "__main__":
    main()
