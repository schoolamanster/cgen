"""Build a satcoin SAT instance from a Bitcoin block header.

Turns the Bitcoin mining problem into a single DIMACS CNF that any SAT
solver can consume. The encoded constraint is the EXACT Bitcoin rule —
"double-SHA-256(header) ≤ target" — not a relaxed leading-zero
approximation. The academic operating assumption is that the SAT solver
takes zero time; under that assumption the rest of the pipeline must be
production-grade so the (hypothetical) solved nonce can be redeemed.

Pipeline (see ./README.md § 4 for the bit-flow diagram):

    1. Read a header JSON from fetch_block.py (includes the 256-bit target).
    2. Call cgen to encode "SHA-256 of the 80-byte header, with the 4-byte
       nonce left free as SAT variables" → CNF₁.
    3. Call cgen to encode "SHA-256 of a 256-bit free message (padded for
       SHA-256), where the message variables will become the splice point"
       → CNF₂. This second encoding is block-independent and is cached
       in .cache/ after the first run.
    4. Splice CNF₁ and CNF₂: identify the named variable H in CNF₁ (the
       first hash's output bits) and named variable M in CNF₂ (the second
       hash's input bits), and rewrite CNF₂'s clauses so that its M
       variables are replaced by CNF₁'s H variables. Renumber CNF₂'s
       remaining variables so they don't collide.
    5. Allocate 256 fresh auxiliary variables (s_0..s_255 — the
       "still-equal-to-target" cascade) and append the clauses that
       encode "double-SHA-256(header) ≤ target" exactly. Bit-by-bit
       leading-equality + first-difference decomposition.
    6. Write the combined CNF.

Endianness note: SHA-256 returns 32 bytes that Bitcoin treats as a 256-bit
integer in *byte-reversed* order. The hash bits as cgen names them in H
are MSB-first within each 32-bit word, and the words are H[0]..H[7] in
that order — which matches SHA-256's serialization. The target value
returned by fetch_block.py is already big-endian as a 256-bit integer;
the comparison "hash ≤ target" is done on integers, sidestepping the
byte-reverse confusion entirely. submit_block.py independently verifies
with hashlib using Bitcoin's actual byte order.

Usage:
    python build_satcoin_cnf.py --header out/header.json --output out/satcoin.cnf
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CGEN_EXE = REPO_ROOT / "cgeno.exe"

# Cache: the second-hash CNF depends only on cgen's encoding of
# "SHA-256 of a 256-bit free message". It does NOT depend on the block.
# So we encode it once and reuse it forever, saving one cgen subprocess
# call (~400 ms) per block we process.
CACHE_DIR = Path(__file__).resolve().parent / ".cache"
SECOND_HASH_CACHE = CACHE_DIR / "second_hash.cnf"


# ---------------------------------------------------------------------------
# Parsing cgen's named-variable definitions
# ---------------------------------------------------------------------------
#
# cgen records named variables in DIMACS comment lines like:
#   c var H = {{45323/31/-2, -45200}, {-45384, 45382/30/-2, -44642}, ...}
#   c var M = {{1/32/1}/8/32, 0x80000000, 0x00000000/6, 0x00000100}
#
# Grammar (informal):
#   group   = '{' element (',' element)* '}'
#   element = base ('/' count ('/' step)?)?
#   base    = group | int | hex | bin
#
# A sequence "X/n/s" generates n elements where the first is X and each
# subsequent one is the previous + s. If the base of the sequence is a
# group, the step applies to every variable inside the group.
#
# We flatten the whole thing into a list of "bit specs". Each bit spec is
# either a signed integer (representing a literal — positive for variable
# v, negative for ¬v) or a string 'C0'/'C1' representing a constant bit.

_TOKEN_RE = re.compile(r"""
    \s+                    | # whitespace (skip)
    (?P<lbrace>\{)         |
    (?P<rbrace>\})         |
    (?P<comma>,)           |
    (?P<slash>/)           |
    (?P<hex>0x[0-9a-fA-F]+)|
    (?P<bin>0b[01]+)       |
    (?P<int>-?\d+)
""", re.VERBOSE)


def _tokenize(s: str):
    tokens = []
    pos = 0
    while pos < len(s):
        m = _TOKEN_RE.match(s, pos)
        if not m:
            raise ValueError(f"Tokenization failed at offset {pos}: {s[pos:pos+30]!r}")
        pos = m.end()
        if m.group().isspace():
            continue
        for name in ("lbrace", "rbrace", "comma", "slash", "hex", "bin", "int"):
            v = m.group(name)
            if v is not None:
                if name == "int":
                    tokens.append(("INT", int(v)))
                else:
                    tokens.append((name.upper(), v))
                break
    return tokens


class _Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def _peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _eat(self, kind=None):
        tok = self.tokens[self.pos]
        if kind and tok[0] != kind:
            raise ValueError(f"Expected {kind}, got {tok}")
        self.pos += 1
        return tok

    def parse_group(self):
        self._eat("LBRACE")
        elements = [self.parse_element()]
        while self._peek() and self._peek()[0] == "COMMA":
            self._eat("COMMA")
            elements.append(self.parse_element())
        self._eat("RBRACE")
        return ("group", elements)

    def parse_element(self):
        base = self.parse_base()
        count, step = 1, 1
        if self._peek() and self._peek()[0] == "SLASH":
            self._eat("SLASH")
            count = self._eat("INT")[1]
            if self._peek() and self._peek()[0] == "SLASH":
                self._eat("SLASH")
                step = self._eat("INT")[1]
        return ("elem", base, count, step)

    def parse_base(self):
        tok = self._peek()
        if tok[0] == "LBRACE":
            return self.parse_group()
        if tok[0] in ("INT", "HEX", "BIN"):
            return self._eat()
        raise ValueError(f"Unexpected token: {tok}")


def _shift_base(base, offset):
    """Apply a sequence step (offset) to every variable in `base`.

    Per cgen spec: stepping shifts variable numbers by +offset. Sign
    (negation) is preserved by shifting the absolute value.
    """
    if base[0] == "INT":
        n = base[1]
        if n > 0:
            return ("INT", n + offset)
        else:
            return ("INT", -((-n) + offset))
    if base[0] in ("HEX", "BIN"):
        return base  # constants don't shift
    if base[0] == "group":
        return ("group", [
            ("elem", _shift_base(b, offset), c, s)
            for (_, b, c, s) in base[1]
        ])
    raise ValueError(f"Unknown base kind: {base[0]}")


def _expand_base(base):
    """Flatten a base (with no surrounding sequence multiplier) to bit specs."""
    if base[0] == "INT":
        return [base[1]]  # signed literal
    if base[0] == "HEX":
        s = base[1][2:]
        out = []
        for ch in s:
            v = int(ch, 16)
            for b in range(3, -1, -1):
                out.append(f"C{(v >> b) & 1}")
        return out
    if base[0] == "BIN":
        return [f"C{int(ch)}" for ch in base[1][2:]]
    if base[0] == "group":
        bits = []
        for elem in base[1]:
            bits.extend(_expand_element(elem))
        return bits
    raise ValueError(f"Unknown base kind: {base[0]}")


def _expand_element(elem):
    """Flatten an element (base + sequence) to bit specs."""
    _, base, count, step = elem
    bits = []
    for i in range(count):
        shifted = _shift_base(base, i * step)
        bits.extend(_expand_base(shifted))
    return bits


def parse_named_var(def_str: str):
    """Parse a 'c var X = {...}' definition's RHS into a list of bit specs.

    Returns a list where each entry is either:
      - a signed int (positive = variable literal, negative = ¬variable)
      - 'C0' or 'C1' (a constant bit)
    """
    tokens = _tokenize(def_str)
    parser = _Parser(tokens)
    parsed = parser.parse_group()
    return _expand_base(parsed)


def read_named_var(cnf_path: Path, var_name: str):
    """Find 'c var <name> = ...' in the CNF header and return the parsed bit list."""
    needle = f"c var {var_name} = "
    with open(cnf_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.startswith(needle):
                return parse_named_var(line[len(needle):].rstrip())
    raise KeyError(f"No 'c var {var_name}' definition in {cnf_path}")


# ---------------------------------------------------------------------------
# Calling cgen for the two encodings
# ---------------------------------------------------------------------------

def _run_cgen(args, *, cwd):
    cmd = [str(CGEN_EXE)] + args
    print(f"[cgen] {' '.join(cmd)}", file=sys.stderr)
    res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if res.returncode != 0:
        sys.stderr.write(res.stdout)
        sys.stderr.write(res.stderr)
        sys.exit(f"cgen failed with exit code {res.returncode}")
    return res


def encode_first_hash(header_hex: str, out_path: Path, out_dir: Path):
    """CNF₁: SHA-256 of the 80-byte header with the nonce left as 32 variables.

    cgen names: M = full 1024-bit padded message (80 bytes + SHA-256 padding),
                H = 256-bit hash output.
    """
    _run_cgen([
        "encode", "SHA256",
        "-vM", f"0x{header_hex}", "pad:sha256", "except:609..640",
        str(out_path),
    ], cwd=out_dir)


def encode_second_hash(out_path: Path, out_dir: Path):
    """CNF₂: SHA-256 of a 256-bit message, with the 256-bit padding pre-baked.

    We can't simply ask cgen to encode "SHA-256 of unknown 256-bit input"
    because cgen wants the full block. So we construct a 512-bit M where:
      - bits 1..256: free (the message — to be spliced in later)
      - bits 257..512: SHA-256 standard padding for a 256-bit message
                       (0x80 followed by zeros, ending with 0x100 = 256
                        as the 64-bit length).

    No `pad:sha256` flag: M is already exactly 512 bits and already includes
    the padding. cgen will compress one block.
    """
    placeholder_msg = "00" * 32  # 32 bytes — will be overwritten by `except`
    # 32-byte padding tail: 0x80 byte, 23 zero bytes, then 8-byte BE length = 256
    pad = "80" + "00" * 23 + "0000000000000100"
    assert len(pad) == 64, f"padding hex must be 64 chars, got {len(pad)}"
    m_hex = placeholder_msg + pad
    _run_cgen([
        "encode", "SHA256",
        "-vM", f"0x{m_hex}", "except:1..256",
        str(out_path),
    ], cwd=out_dir)


# ---------------------------------------------------------------------------
# Splicing CNF₂ onto CNF₁
# ---------------------------------------------------------------------------

def _parse_dimacs_header(cnf_path: Path):
    """Find the 'p cnf <n_vars> <n_clauses>' line and return (n_vars, n_clauses)."""
    with open(cnf_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.startswith("p cnf"):
                _, _, n_vars, n_clauses = line.split()
                return int(n_vars), int(n_clauses)
    raise ValueError(f"No 'p cnf' header in {cnf_path}")


def _iter_clauses(cnf_path: Path):
    """Yield each clause as a list of signed ints (excluding the trailing 0)."""
    with open(cnf_path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip() or line.startswith("c") or line.startswith("p"):
                continue
            literals = [int(t) for t in line.split() if t]
            if literals and literals[-1] == 0:
                literals.pop()
            yield literals


def _target_le_clauses(h2_remapped: list, target_int: int, base_var: int):
    """Encode 'h2 <= target' exactly as CNF clauses.

    h2_remapped : 256 signed ints, MSB first, in the combined CNF's namespace.
    target_int  : 256-bit unsigned int — the network's target for this block.
    base_var    : next free variable number; we'll allocate 256 fresh aux vars
                  s_0..s_255 starting here. (s_i means "h[0..i-1] == t[0..i-1]".)

    Returns (clauses, n_aux). The encoding follows the standard
    leading-equality + first-difference decomposition:

        s_0 = TRUE
        for i = 0..255:
            if t_i = 0:  forbid (s_i AND h_i = 1)  ; clause (-s_i, -h_i)
            for i < 255: define s_{i+1} ↔ (s_i AND (h_i XNOR t_i))

    'h <= t' holds iff for every i where s_i is true, either we never
    encounter t_i=0 with h_i=1 (we either stay equal or transition to h<t
    on a t_i=1 bit). The encoding makes any h>t assignment unsatisfiable.
    """
    if all(isinstance(b, int) for b in h2_remapped) is False:
        sys.exit("h2 has non-variable bits; this encoding assumes all 256 H₂ bits are SAT variables.")
    if not (0 <= target_int < (1 << 256)):
        sys.exit("target_int out of 256-bit range")

    target_bits = [(target_int >> (255 - i)) & 1 for i in range(256)]
    s = [base_var + i for i in range(256)]  # s_0..s_255
    clauses: list[list[int]] = []

    # s_0 := TRUE (unit clause).
    clauses.append([s[0]])

    for i in range(256):
        h_i = h2_remapped[i]
        s_i = s[i]
        if target_bits[i] == 0:
            # If we're still equal and the hash has a 1 here, hash > target.
            # Forbid that combination.
            clauses.append([-s_i, -h_i])
        if i < 255:
            s_next = s[i + 1]
            # "Stayed equal" at bit i: t_i = h_i.
            #   t_i = 1 → x = h_i  (we stayed equal iff h_i was also 1)
            #   t_i = 0 → x = -h_i (we stayed equal iff h_i was also 0)
            x = h_i if target_bits[i] == 1 else -h_i
            # s_next ↔ (s_i ∧ x): three clauses.
            clauses.append([-s_next, s_i])
            clauses.append([-s_next, x])
            clauses.append([-s_i, -x, s_next])

    return clauses, 256


def splice_cnfs(cnf1_path: Path, cnf2_path: Path, out_path: Path,
                h1_bits: list, m2_bits: list, h2_bits: list,
                h2_bits_bitcoin_order: list,
                target_int: int):
    """Concatenate CNF₁ and CNF₂ with M₂ substituted by H₁, then encode
    'hash <= target' exactly.

    `h2_bits_bitcoin_order` is h2 reordered to match Bitcoin's
    byte-reversed integer interpretation, MSB first. This is what we
    actually compare to the target.

    Variable renumbering:
      - CNF₁'s variables 1..N₁ keep their numbers.
      - CNF₂'s M variables (the first 256 entries of m2_bits) are replaced
        by the corresponding entries of h1_bits (sign-aware).
      - CNF₂'s remaining variables 257..N₂ are shifted by (N₁ - 256), so
        they land in N₁+1..N₁+(N₂-256). No collision with CNF₁.

    Target constraint:
      - 256 fresh aux variables (s_0..s_255) track the "still-equal" prefix
        between the hash and the network target. A cascade of CNF clauses
        forces any model with hash > target to be UNSAT. This is the exact
        Bitcoin rule, not an approximation.
    """
    n1_vars, n1_clauses = _parse_dimacs_header(cnf1_path)
    n2_vars, n2_clauses = _parse_dimacs_header(cnf2_path)

    if len(m2_bits) < 256:
        sys.exit(f"CNF₂ M has {len(m2_bits)} bits, need at least 256")
    if len(h1_bits) != 256:
        sys.exit(f"CNF₁ H has {len(h1_bits)} bits, expected 256")
    if len(h2_bits) != 256:
        sys.exit(f"CNF₂ H has {len(h2_bits)} bits, expected 256")

    subst = {}
    for i in range(256):
        m2 = m2_bits[i]
        h1 = h1_bits[i]
        if not isinstance(m2, int) or not isinstance(h1, int):
            sys.exit(f"Bit {i}: M₂={m2!r}, H₁={h1!r} — splice expects both to be variable literals")
        m2_var = abs(m2)
        m2_sign = 1 if m2 > 0 else -1
        subst[m2_var] = m2_sign * h1

    shift = n1_vars - 256

    def remap_lit(L):
        v = abs(L)
        sign = 1 if L > 0 else -1
        if v in subst:
            return sign * subst[v]
        return sign * (v + shift)

    h2_remapped = [
        b if not isinstance(b, int) else remap_lit(b)
        for b in h2_bits
    ]
    h2_bitcoin_remapped = [
        b if not isinstance(b, int) else remap_lit(b)
        for b in h2_bits_bitcoin_order
    ]

    # Combined variable count, before allocating target-encoding aux vars.
    n_pre_target = n1_vars + (n2_vars - 256)

    target_clauses, n_target_aux = _target_le_clauses(
        h2_bitcoin_remapped, target_int, base_var=n_pre_target + 1)

    n_total_vars = n_pre_target + n_target_aux
    n_total_clauses = n1_clauses + n2_clauses + len(target_clauses)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as out:
        out.write("c Satcoin spliced CNF — hash <= target (exact)\n")
        out.write(f"c   CNF1: {cnf1_path.name} ({n1_vars} vars, {n1_clauses} cls)\n")
        out.write(f"c   CNF2: {cnf2_path.name} ({n2_vars} vars, {n2_clauses} cls)\n")
        out.write(f"c   M2 (256 bits) substituted with H1 (sign-aware)\n")
        out.write(f"c   CNF2 non-M vars shifted by {shift}\n")
        out.write(f"c   target = 0x{target_int:064x}\n")
        out.write(f"c   target encoding: {n_target_aux} aux vars (s_0..s_255), {len(target_clauses)} clauses\n")
        out.write("c nonce_vars 1..32 (CNF1 numbering, preserved in combined)\n")
        out.write(f"c h2_remapped {' '.join(str(b) for b in h2_remapped)}\n")
        out.write(f"p cnf {n_total_vars} {n_total_clauses}\n")

        # CNF₁ clauses unchanged.
        with open(cnf1_path, "r", encoding="utf-8") as f1:
            for line in f1:
                if not line.strip() or line.startswith("c") or line.startswith("p"):
                    continue
                out.write(line)

        # CNF₂ clauses with literals remapped.
        for clause in _iter_clauses(cnf2_path):
            out.write(" ".join(str(remap_lit(L)) for L in clause))
            out.write(" 0\n")

        # 'hash <= target' cascade.
        for clause in target_clauses:
            out.write(" ".join(str(L) for L in clause))
            out.write(" 0\n")

    print(f"Wrote {out_path} ({n_total_vars} vars, {n_total_clauses} clauses)", file=sys.stderr)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--header", required=True, type=Path,
                   help="JSON file from fetch_block.py")
    p.add_argument("--output", required=True, type=Path,
                   help="Where to write the final combined CNF")
    p.add_argument("--target", type=str, default=None,
                   help="Override the network target as a 256-bit hex string "
                        "(default: use the 'bits'-derived target from the header). "
                        "Useful for testing the encoding against synthetic targets.")
    p.add_argument("--pin-nonce", type=str, default=None,
                   help="Pin the nonce to a specific 8-hex-char value (e.g. '00000000') "
                        "by appending 32 unit clauses to the output CNF. The resulting "
                        "CNF is satisfiable iff that specific nonce satisfies the target "
                        "for this header. NOTE: pinning the nonce to 0x00000000 does NOT "
                        "make the hash zero — the hash is deterministic given the header.")
    p.add_argument("--keep-intermediates", action="store_true",
                   help="Don't delete CNF1/CNF2 after splicing")
    args = p.parse_args()

    if not CGEN_EXE.exists():
        sys.exit(f"cgen binary not found at {CGEN_EXE}. Build the project first.")

    with open(args.header) as f:
        header = json.load(f)
    header_hex = header["raw_header_hex"]
    if len(header_hex) != 160:
        sys.exit(f"Header hex must be 160 chars, got {len(header_hex)}")

    work_dir = args.output.parent.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    cnf1 = work_dir / "_satcoin_cnf1.cnf"
    cnf2 = work_dir / "_satcoin_cnf2.cnf"

    # CNF₂ is block-independent — cache it. First run encodes + caches;
    # all subsequent runs (and all parallel block processing) reuses.
    CACHE_DIR.mkdir(exist_ok=True)
    if not SECOND_HASH_CACHE.exists():
        # First-time setup: encode CNF₁ and CNF₂ in parallel since they
        # don't depend on each other. Saves another ~400 ms on cold start.
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(encode_first_hash, header_hex, cnf1, work_dir)
            f2 = pool.submit(encode_second_hash, SECOND_HASH_CACHE, CACHE_DIR)
            f1.result(); f2.result()
    else:
        encode_first_hash(header_hex, cnf1, work_dir)

    # Always copy/symlink the cached CNF₂ into the work dir so cleanup logic
    # (the --keep-intermediates flag) treats both files uniformly.
    import shutil
    shutil.copyfile(SECOND_HASH_CACHE, cnf2)

    h1_bits = read_named_var(cnf1, "H")
    m2_bits = read_named_var(cnf2, "M")
    h2_bits = read_named_var(cnf2, "H")

    target_str = args.target if args.target else header["fields"]["target"]
    if target_str.startswith("0x") or target_str.startswith("0X"):
        target_str = target_str[2:]
    try:
        target_int = int(target_str, 16)
    except ValueError:
        sys.exit(f"Invalid target hex: {target_str!r}")

    # Endianness reconciliation. Bitcoin compares
    #   int.from_bytes(sha256_output_bytes, 'little')  ≤  target
    # cgen's flattened H bits represent the SHA-256 output bytes in their
    # *native* (big-endian) order: bit i of h2_bits corresponds to bit i of
    # int.from_bytes(sha256_output_bytes, 'big').
    # We must therefore *reorder* h2_bits before comparison: take 8-bit
    # chunks (one per output byte), reverse the chunk order, concatenate.
    # The result is the bit-by-bit representation of Bitcoin's integer,
    # MSB first, which is what _target_le_clauses expects.
    chunks = [h2_bits[8 * k : 8 * k + 8] for k in range(32)]
    h2_bitcoin_order = []
    for chunk in reversed(chunks):
        h2_bitcoin_order.extend(chunk)
    if len(h2_bitcoin_order) != 256:
        sys.exit(f"Reordered H has {len(h2_bitcoin_order)} bits, expected 256")

    splice_cnfs(cnf1, cnf2, args.output,
                h1_bits=h1_bits, m2_bits=m2_bits,
                h2_bits=h2_bits, h2_bits_bitcoin_order=h2_bitcoin_order,
                target_int=target_int)

    # Optional nonce pin. Appends 32 unit clauses to the output, fixing
    # variables 1..32 to the bits of the requested nonce value (MSB-first
    # within each byte, header byte 76 → vars 1..8, etc.).
    if args.pin_nonce:
        nonce_hex = args.pin_nonce.removeprefix("0x").removeprefix("0X")
        if len(nonce_hex) != 8:
            sys.exit(f"--pin-nonce must be exactly 8 hex chars (got {len(nonce_hex)})")
        try:
            nonce_bytes = bytes.fromhex(nonce_hex)
        except ValueError:
            sys.exit(f"--pin-nonce is not valid hex: {args.pin_nonce!r}")
        units = []
        for byte_i, b in enumerate(nonce_bytes):
            for bit_in_byte in range(7, -1, -1):
                var = byte_i * 8 + (7 - bit_in_byte) + 1
                sign = 1 if (b >> bit_in_byte) & 1 else -1
                units.append(sign * var)
        # Rewrite the CNF header line in place to account for the extra clauses,
        # then append the unit clauses at the end.
        with open(args.output, "r") as f:
            lines = f.readlines()
        for i, line in enumerate(lines):
            if line.startswith("p cnf"):
                parts = line.split()
                old_cls = int(parts[3])
                lines[i] = f"p cnf {parts[2]} {old_cls + len(units)}\n"
                break
        lines.append(f"c --pin-nonce {nonce_hex}: 32 unit clauses below\n")
        for u in units:
            lines.append(f"{u} 0\n")
        with open(args.output, "w") as f:
            f.writelines(lines)
        print(f"Pinned nonce to 0x{nonce_hex} (added 32 unit clauses)", file=sys.stderr)

    if not args.keep_intermediates:
        cnf1.unlink(missing_ok=True)
        cnf2.unlink(missing_ok=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
