"""Sanity-check: pin nonce to the known genesis value and run the solver.

If the encoding is correct, the solver returns SAT in seconds — because
the known genesis nonce IS a valid Bitcoin solution for genesis's real target.
If the solver says UNSAT (or runs forever), the CNF is wrong.
"""

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
OUT.mkdir(exist_ok=True)

# Genesis nonce as 32 bits MSB-first within each byte, in network/SHA-256 byte order.
# Bytes 76-79 of genesis header = 0x1d 0xac 0x2b 0x7c
NONCE_BYTES = bytes.fromhex("1dac2b7c")
unit_lits = []
for byte_i, b in enumerate(NONCE_BYTES):
    for bit_in_byte in range(7, -1, -1):  # MSB first
        var = 8 * byte_i + (7 - bit_in_byte) + 1   # var 1 = MSB of byte 0
        sign = 1 if (b >> bit_in_byte) & 1 else -1
        unit_lits.append(sign * var)

# Take the existing genesis CNF, append unit pin clauses.
src = OUT / "s.cnf"
if not src.exists():
    sys.exit(f"Run build_satcoin_cnf.py first to produce {src}")
dst = OUT / "s_pinned.cnf"

with open(src) as f, open(dst, "w") as o:
    for line in f:
        if line.startswith("p cnf"):
            parts = line.split()
            n_vars = int(parts[2]); n_cls = int(parts[3])
            o.write(f"p cnf {n_vars} {n_cls + len(unit_lits)}\n")
        else:
            o.write(line)
    for L in unit_lits:
        o.write(f"{L} 0\n")

print(f"Pinned 32 nonce bits in {dst} ({n_cls + len(unit_lits)} clauses)")
print(f"Nonce bit assignment: {' '.join(str(L) for L in unit_lits)}")

cms = HERE.parent.parent / "tools" / "cryptominisat" / "cryptominisat5.exe"
print(f"\nSolving with target = real genesis target...")
res = subprocess.run([str(cms), "--verb", "0", str(dst)], capture_output=True, text=True)
print(res.stdout[:2000])
print("STDERR:", res.stderr[:500] if res.stderr else "(none)")
print("Exit code:", res.returncode)
