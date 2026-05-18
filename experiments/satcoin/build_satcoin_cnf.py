"""Build a satcoin SAT instance from a Bitcoin block header.

This script is the centerpiece: it turns the Bitcoin mining problem into a
single DIMACS CNF that any SAT solver can consume.

Pipeline (see ./README.md § 4 for the detailed bit-flow diagram):

    1. Read a header JSON from fetch_block.py.
    2. Call cgen to encode "SHA-256 of the 80-byte header, with the 4-byte
       nonce left free as SAT variables" → CNF₁.
    3. Call cgen to encode "SHA-256 of a 256-bit free message (padded for
       SHA-256), where the message variables will become the splice point"
       → CNF₂.
    4. Splice CNF₁ and CNF₂: identify the named variable H in CNF₁ (the
       first hash's output bits) and named variable M in CNF₂ (the second
       hash's input bits), and rewrite CNF₂'s clauses so that its M
       variables are replaced by CNF₁'s H variables. Renumber CNF₂'s
       remaining variables so they don't collide.
    5. Append unit clauses fixing the top N bits of CNF₂'s H (the final
       hash output) to zero. This is the difficulty target.
    6. Write the combined CNF.

Endianness note: this script enforces "top N bits of SHA-256 output = 0"
interpreting SHA-256's output as a big-endian 256-bit integer. Bitcoin's
actual target comparison uses the *byte-reversed* hash. For the academic
question "can SAT find a preimage with N leading bits zero", these are
structurally equivalent. For *exact* Bitcoin difficulty matching, see
submit_block.py's verification step, which uses standard Python hashlib.

Usage:
    python build_satcoin_cnf.py --header out/header.json \
        --difficulty-bits 8 --output out/satcoin.cnf
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


def splice_cnfs(cnf1_path: Path, cnf2_path: Path, out_path: Path,
                h1_bits: list, m2_bits: list, h2_bits: list,
                difficulty_bits: int):
    """Concatenate CNF₁ and CNF₂ with M₂ substituted by H₁, then pin H₂.

    Variable renumbering:
      - CNF₁'s variables 1..N₁ keep their numbers.
      - CNF₂'s M variables (the first 256 entries of m2_bits) are replaced
        by the corresponding entries of h1_bits (sign-aware).
      - CNF₂'s remaining variables 257..N₂ are shifted by (N₁ - 256), so
        they land in N₁+1..N₁+(N₂-256). No collision with CNF₁.

    Difficulty target:
      - The top `difficulty_bits` entries of h2_bits (after remapping into
        CNF₁'s namespace via the same shift) are pinned to 0 with unit
        clauses. h2_bits[0] is the MSB of the first H word.
    """
    n1_vars, n1_clauses = _parse_dimacs_header(cnf1_path)
    n2_vars, n2_clauses = _parse_dimacs_header(cnf2_path)

    # Verify the splice shape.
    if len(m2_bits) < 256:
        sys.exit(f"CNF₂ M has {len(m2_bits)} bits, need at least 256")
    if len(h1_bits) != 256:
        sys.exit(f"CNF₁ H has {len(h1_bits)} bits, expected 256")
    if len(h2_bits) != 256:
        sys.exit(f"CNF₂ H has {len(h2_bits)} bits, expected 256")
    if difficulty_bits < 0 or difficulty_bits > 256:
        sys.exit(f"difficulty-bits must be in [0, 256], got {difficulty_bits}")

    # Build CNF₂-variable → substitute-literal map.
    # m2_bits[i] is a signed int (positive var, negative ¬var) referencing
    # CNF₂'s variable space; we want any literal pointing at the same
    # variable to be replaced with the matching h1_bits[i] in CNF₁'s space.
    subst = {}  # cnf2_var (positive) -> signed literal in combined namespace
    for i in range(256):
        m2 = m2_bits[i]
        h1 = h1_bits[i]
        if not isinstance(m2, int) or not isinstance(h1, int):
            sys.exit(f"Bit {i}: M₂={m2!r}, H₁={h1!r} — splice expects both to be variable literals")
        m2_var = abs(m2)
        m2_sign = 1 if m2 > 0 else -1
        # Whatever h1 is, it's signed in CNF₁'s namespace. If M₂'s literal
        # at this bit was negated, we invert h1's sign so that the *value*
        # of the bit is preserved.
        subst[m2_var] = m2_sign * h1

    shift = n1_vars - 256

    def remap_lit(L):
        v = abs(L)
        sign = 1 if L > 0 else -1
        if v in subst:
            return sign * subst[v]
        return sign * (v + shift)

    # Compute the new variable numbers of H₂ (the final output bits).
    h2_remapped = [
        b if not isinstance(b, int) else remap_lit(b)
        for b in h2_bits
    ]

    # Write the combined CNF.
    n_total_vars = n1_vars + (n2_vars - 256)
    extra_unit_clauses = sum(1 for i in range(difficulty_bits)
                              if isinstance(h2_remapped[i], int))
    n_total_clauses = n1_clauses + n2_clauses + extra_unit_clauses

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as out:
        out.write(f"c Satcoin spliced CNF\n")
        out.write(f"c   CNF1: {cnf1_path.name} ({n1_vars} vars, {n1_clauses} cls)\n")
        out.write(f"c   CNF2: {cnf2_path.name} ({n2_vars} vars, {n2_clauses} cls)\n")
        out.write(f"c   M2 (256 bits) substituted with H1 (sign-aware)\n")
        out.write(f"c   CNF2 non-M vars shifted by {shift}\n")
        out.write(f"c   Top {difficulty_bits} bits of H2 pinned to 0\n")
        # Annotation comments that submit_block.py reads to recover the nonce.
        # The first 32 entries of CNF1's M (after parsing) won't be at fixed
        # positions; instead the 32 nonce variables are exactly the first 32
        # positive ints assigned by cgen → they are variables 1..32 in CNF1.
        # We record this here for downstream tools.
        out.write(f"c nonce_vars 1..32 (CNF1 numbering, preserved in combined)\n")
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

        # Difficulty pins: top `difficulty_bits` of H₂ = 0.
        for i in range(difficulty_bits):
            b = h2_remapped[i]
            if isinstance(b, int):
                # Negate to force the bit to 0 (a positive literal asserts the
                # variable is TRUE, so the negation asserts FALSE).
                out.write(f"{-b} 0\n")
            # If a constant bit, it's already 0 or 1 — nothing to assert.
            # (For SHA-256, H bits are essentially never determined as constants
            # in our partially-fixed encoding, but defending against it is cheap.)

    print(f"Wrote {out_path} ({n_total_vars} vars, {n_total_clauses} clauses)", file=sys.stderr)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--header", required=True, type=Path,
                   help="JSON file from fetch_block.py")
    p.add_argument("--difficulty-bits", type=int, default=8,
                   help="How many top bits of the final hash must be zero (default: 8)")
    p.add_argument("--output", required=True, type=Path,
                   help="Where to write the final combined CNF")
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

    splice_cnfs(cnf1, cnf2, args.output,
                h1_bits=h1_bits, m2_bits=m2_bits, h2_bits=h2_bits,
                difficulty_bits=args.difficulty_bits)

    if not args.keep_intermediates:
        cnf1.unlink(missing_ok=True)
        cnf2.unlink(missing_ok=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
