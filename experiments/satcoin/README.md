# Satcoin Experiment: Bitcoin Mining as a SAT Problem

This directory is the academic experiment described by Jonathan Heusser's
["SAT Solving — An Alternative to Brute Force Bitcoin Mining" (2013)](https://jheusser.github.io/2013/02/03/satcoin.html),
rebuilt on top of CGen for current SHA-256 encoding and modern tooling.

The point of the experiment is **not to mine Bitcoin**. It is to translate a
real-world problem — finding a nonce that makes a block hash valid — into
the most general NP-complete problem (Boolean satisfiability) and study
what that translation looks like.

If by some miracle a SAT solver did find a winning nonce, the infrastructure
in this directory is wired up to submit the block to the network. That path
is real but the probability of taking it is mathematically indistinguishable
from zero — see [§ Reality Check](#reality-check) below.

---

## Table of contents

1. [What problem are we actually solving?](#what-problem)
2. [Why SAT? Why is it academically interesting?](#why-sat)
3. [Reality check on "competing with miners"](#reality-check)
4. [Pipeline overview — bits in, bits out](#pipeline)
5. [What each piece of the codebase does](#code-tour)
6. [How to run it](#how-to-run)
7. [Difficulty levels — what's tractable, what's hopeless](#difficulty)
8. [The redemption path](#redemption)
9. [What's still left to you](#left-to-you)

---

<a id="what-problem"></a>
## 1. What problem are we actually solving?

A **Bitcoin block header** is 80 bytes laid out like this:

```
offset  size  field
   0      4   version
   4     32   prev block hash       (the hash of the block before this one)
  36     32   merkle root           (a hash representing every transaction in this block)
  68      4   timestamp
  72      4   bits                  (encoded difficulty target)
  76      4   nonce                 ← the only "free" field
```

**Bitcoin mining is solving this:**

> Find a 4-byte `nonce` (and optionally tweak `timestamp` or the coinbase
> transaction) such that
>
>     SHA256(SHA256(80-byte header)) ≤ target,
>
> where `target` is a 256-bit number derived from the `bits` field.

The hash is interpreted as a 256-bit unsigned integer in little-endian byte
order. "≤ target" is overwhelmingly determined by **how many leading zero
bits** the hash has — the more zeros at the front, the smaller the number.
This is why difficulty is colloquially described in terms of "leading zeros."

**Note: Bitcoin uses *double* SHA-256.** The header is hashed once, the
resulting 32 bytes are hashed again. This isn't for security exactly; it's a
defense against length-extension attacks that affect single SHA-256 in some
constructions. For us it means our CNF encoding has to chain two SHA-256
invocations.

So the satcoin question is:

> Can we phrase "find a nonce such that double-SHA256(header) has ≥ N leading
> zero bits" as a Boolean satisfiability problem, and does a SAT solver
> handle it any better than brute-forcing nonces one by one?

<a id="why-sat"></a>
## 2. Why SAT? Why is it academically interesting?

**SAT** stands for **Boolean satisfiability**. A SAT problem is a giant
boolean formula in **CNF** (conjunctive normal form — an AND of ORs):

```
(x1 ∨ ¬x3 ∨ x7) ∧ (¬x1 ∨ x2) ∧ (x3 ∨ x4 ∨ ¬x5) ∧ …
```

A SAT *solver* is a program that, given such a formula, either finds an
assignment of true/false to each `xi` that makes the whole thing true
("SAT"), or proves no such assignment exists ("UNSAT").

SAT is the canonical **NP-complete** problem: every problem in NP can be
mechanically translated into a SAT instance. Modern solvers (MiniSat,
CryptoMiniSat, Kissat, CaDiCaL) use the **CDCL** algorithm — Conflict-Driven
Clause Learning. The short version: they make guesses, propagate the
consequences, and when they hit a contradiction they learn a new clause
that prevents repeating the mistake. It works astonishingly well on many
"hard-looking" problems — circuit verification, scheduling, planning.

**The crypto twist:** SHA-256 is *designed* to defeat exactly the kind of
inference SAT solvers thrive on. Its **avalanche property** says that
flipping one input bit flips on average half the output bits in a
seemingly-random pattern. When you fix the output of SHA-256 and ask "what
input produced this?", the solver gets almost no propagation: knowing one
output bit barely constrains any input bit.

So the academic finding from Heusser's satcoin (and replicated since): for
Bitcoin-flavored SHA-256 inversion, **SAT solvers perform essentially like
brute force**. Sometimes a bit better with crypto-specific tricks (XOR
clauses, Gaussian elimination — CryptoMiniSat's specialties), but never
exponentially better.

**This is the punchline.** It's a real, measurable demonstration of why
SHA-256 is preimage-resistant. You're not "trying to break Bitcoin." You're
showing that even general-purpose AI-flavored search algorithms can't crack
a designed-to-be-random function.

<a id="reality-check"></a>
## 3. Reality check on "competing with miners"

If you're imagining the lottery angle — "maybe I get lucky" — here are the
numbers, since `2026-05-18`:

| Quantity | Value |
|---|---|
| Bitcoin network hashrate | ~600 EH/s = 6 × 10²⁰ SHA-256/sec |
| Modern laptop CPU brute-force hashrate | ~10⁷ SHA-256/sec |
| ASIC vs. CPU brute-force ratio | ~10¹³ |
| CPU brute-force vs. CPU SAT (per "attempt") | another ~10³–10⁵ disadvantage |
| Current difficulty (≈ leading zero bits in hash target) | ~78 |
| Expected SHA-256 evaluations per valid block | ~2⁷⁸ ≈ 3 × 10²³ |
| P(your laptop solves the next block in 10 minutes) | ~10⁻²² |
| P(winning Powerball with one ticket) | ~10⁻⁸ |

You can run the pipeline. CryptoMiniSat will sit there making no
measurable progress. It will not terminate before the universe ends. That
is fine — *that is the experiment*. You are measuring the *absence* of a
shortcut.

For results you can actually publish on a class deadline: see
[§ Difficulty levels](#difficulty) for tractable reduced-difficulty regimes.

<a id="pipeline"></a>
## 4. Pipeline overview — bits in, bits out

Here is the full flow, byte by byte:

```
┌──────────────────────────────────────────────────────────────────────────┐
│                              FETCH                                       │
│  blockstream.info API  ──HTTP──▶  80-byte block header (hex)             │
│                                  e.g. genesis:                           │
│                                  01000000 00000000…00 nonce              │
│                                  └─76 bytes fixed─┘└4B free┘             │
└──────────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                              ENCODE (first SHA-256)                      │
│  cgen encode SHA256 -vM <80 hex bytes> except:609..640 pad:sha256        │
│                                                                          │
│  - 76 bytes of header → 608 fixed bits                                   │
│  - 4 bytes of nonce  → 32 free bits (SAT variables)                      │
│  - SHA-256 padding   → cgen adds it automatically                        │
│  - Two 512-bit blocks of compression                                     │
│                                                                          │
│  output: CNF₁ (DIMACS file)                                              │
│    - ~50,000 variables, ~250,000 clauses                                 │
│    - Named variable H₁ = bits of the first hash output (256 bits)        │
└──────────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                              ENCODE (second SHA-256)                     │
│  cgen encode SHA256 (with free 256-bit message) → CNF₂                   │
│                                                                          │
│  - Input: 256 free bits = SAT variables                                  │
│  - SHA-256 padding to 512 bits (one block)                               │
│  - One block of compression                                              │
│                                                                          │
│  output: CNF₂                                                            │
│    - ~25,000 variables, ~125,000 clauses                                 │
│    - Named variable H₂ = bits of the second hash output (256 bits)       │
│    - Named variable M  = bits of the message (256 bits, free)            │
└──────────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                              SPLICE                                      │
│  build_satcoin_cnf.py                                                    │
│                                                                          │
│  - Renumber CNF₂'s variables so they don't collide with CNF₁'s           │
│  - Identify H₁'s variable numbers in CNF₁                                │
│  - Identify M's  variable numbers in CNF₂                                │
│  - Replace M's variable numbers with H₁'s variable numbers everywhere    │
│    in CNF₂'s clauses                                                     │
│  - Concatenate clauses                                                   │
│                                                                          │
│  output: CNF₁₂ — one CNF whose solution is a nonce making                │
│         double-SHA-256(header) be any specific 256-bit value             │
└──────────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                              PIN TARGET                                  │
│                                                                          │
│  Append unit clauses fixing the top N bits of H₂ to 0:                   │
│      (-h2_1) (-h2_2) ... (-h2_N)                                         │
│  N = number of leading zero bits required (the difficulty)               │
│                                                                          │
│  output: CNF_final — "find a nonce such that double-SHA-256(header)      │
│                       has at least N leading zero bits"                  │
└──────────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                              SOLVE  (the part we don't expect to win)    │
│  cryptominisat5 cnf_final.dimacs                                         │
│                                                                          │
│  - Will print "s SATISFIABLE" + assignment, or run forever               │
│  - For real Bitcoin difficulty: runs forever                             │
│  - For reduced difficulty (e.g. N=8): solves in seconds                  │
└──────────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼ (if SAT)
┌──────────────────────────────────────────────────────────────────────────┐
│                              REDEEM                                      │
│  submit_block.py                                                         │
│                                                                          │
│  - Parse solver's variable assignment to extract the nonce               │
│  - Verify locally: does double-SHA-256(header with this nonce) actually  │
│    meet the target?                                                      │
│  - Construct full block hex (header + transactions)                      │
│  - Submit via:                                                           │
│      bitcoin-cli submitblock <hex>   (if local node)                     │
│      OR Stratum to a public mining pool                                  │
└──────────────────────────────────────────────────────────────────────────┘
```

<a id="code-tour"></a>
## 5. What each piece of the codebase does

### Outside this directory (`../../`)

| Path | Role |
|---|---|
| `../../cgen` / `cgeno.exe` | The encoder. Reads a SHA-1 or SHA-256 specification with some bits fixed, writes a CNF/DIMACS file. Written in C++ by Volodymyr Skladanivskyy. Authored without Bitcoin in mind — we're using it as a general SHA-to-CNF compiler. |
| `../../makefile` | Builds cgen with plain g++. Use `mingw32-make` on Windows. |
| `../../CMakeLists.txt` | Equivalent to the makefile, but in CMake. Lets CLion open the project natively. |
| `../../tools/cryptominisat/` | The SAT solver. Downloaded separately, not committed. |

### This directory (`experiments/satcoin/`)

| File | Role |
|---|---|
| `README.md` | This document. |
| `fetch_block.py` | Pulls an 80-byte block header from blockstream.info's public REST API. Supports fetching by height, by hash, or "the current chain tip." Optimized for speed — single HTTP call, no auth, no rate limit issues at low volume. |
| `build_satcoin_cnf.py` | The orchestration. Calls cgen twice (once per SHA-256), splices the two CNFs together, applies the difficulty target as unit clauses, writes a single solver-ready DIMACS file. This is the "transformation" step — Bitcoin problem in, SAT problem out. |
| `submit_block.py` | The redemption path. Takes a SAT solver's `s SATISFIABLE` output, parses out the nonce, verifies the resulting block locally with double-SHA-256, and (if a Bitcoin Core node is configured) calls `bitcoin-cli submitblock` to broadcast it. Without a node it stops at "valid block constructed, ready to submit." |
| `tests/test_genesis.py` | Sanity test using the Bitcoin genesis block (height 0). Difficulty is low enough that the encoded problem is solvable in seconds with the nonce *blanked out and re-solved*. Verifies the pipeline produces the correct intermediate and final hashes. |

<a id="how-to-run"></a>
## 6. How to run it

Prerequisites (one-time):
- `cgeno.exe` built from the repo root (the makefile or CLion's CMake will produce it)
- `cryptominisat5.exe` downloaded to `../../tools/cryptominisat/`
- Python 3.8+ on PATH (for `fetch_block.py` and the orchestrator)

```powershell
# From experiments/satcoin/

# 1) Fetch a block. Examples:
python fetch_block.py --height 0          > out/header.json    # genesis
python fetch_block.py --height 100000     > out/header.json    # 2010-era easy block
python fetch_block.py --tip               > out/header.json    # latest block on chain

# 2) Build the spliced CNF with a difficulty target.
python build_satcoin_cnf.py `
    --header out/header.json `
    --difficulty-bits 8 `
    --output out/satcoin.cnf

# 3) Hand it to the solver.
..\..\tools\cryptominisat\cryptominisat5.exe out/satcoin.cnf

# 4) (Only if you ever see "s SATISFIABLE") parse + verify + optionally submit.
python submit_block.py --header out/header.json --solver-output <solver_log>
```

<a id="difficulty"></a>
## 7. Difficulty levels — what's tractable, what's hopeless

`--difficulty-bits N` controls how many leading bits of the second hash
must be zero. The expected number of nonce attempts to find one is ~2ᴺ.

| N (target zeros) | Expected attempts | Hardware needed |
|---|---|---|
| 1 – 12 | seconds | This is your sanity-check regime. Use these for debugging the pipeline. |
| 16 – 24 | minutes to hours on a laptop | Reasonable for a class demo. Real measurements possible. |
| 28 – 36 | days to weeks on a laptop | Doable if you let it run. Useful for the actual Heusser-style runtime-vs-difficulty curve. |
| 40+ | infeasible on a laptop | Skip. |
| ~78 | infeasible on humanity | Current real Bitcoin. |

The pipeline doesn't care which you pick — but if you set `--difficulty-bits
78` expecting an answer, you will not get one. Set it for the *measurement*
you want, not because that's what "real" mining uses.

<a id="redemption"></a>
## 8. The redemption path

If you ran the pipeline against a real, currently-unmined block template
and the solver ever returned "s SATISFIABLE", here is what happens:

1. **Parse the assignment.** CryptoMiniSat outputs `v 1 -2 3 -4 …` — one
   line per variable, signed (positive = true, negative = false). The
   nonce bits are at known variable numbers (recorded in CNF₁'s
   `c var M = {…}` comment). `submit_block.py` extracts them.
2. **Verify locally.** Run a normal Python `hashlib.sha256(...)` twice on
   the reconstructed header. Confirm the result has the required leading
   zeros. If it doesn't, the pipeline has a bug; abort.
3. **Reconstruct the full block.** A block isn't just a header — it's
   header + coinbase transaction + every other transaction. To assemble
   the full block hex, you need the same transaction list you committed
   to in the merkle root. This is why **real live mining requires a
   Bitcoin Core node**: `getblocktemplate` gives you the transaction
   list and merkle root atomically, so the header you hash matches the
   block you submit.
4. **Submit.** `bitcoin-cli submitblock <hex>` is one call. The node
   validates and gossips. If accepted, the network sees a new block at
   the chain tip. Your coinbase address (which had to be in the coinbase
   transaction, which had to be in the merkle root, which was hashed) is
   now ~3.125 BTC richer, spendable after 100 confirmations (~16 hours).

**This entire path is dead code in practice.** The infrastructure exists
because that was your spec — to be *ready* to redeem if the unimaginable
happens. The code is small and self-contained. You will not exercise it.

<a id="left-to-you"></a>
## 9. What's still left to you

I've built the **fetch** and **encode/splice** and **redemption-prep**
pieces. What's left for you:

1. **Choose what to learn from this.** The most defensible class outcome
   is a plot: x-axis = `--difficulty-bits`, y-axis = solver wall-clock
   time, two lines (CryptoMiniSat vs. brute force in Python). The
   crossover (or lack of one) is the result. Try N=4, 8, 12, 16, 20.
2. **Read the CDCL paper or a CDCL tutorial.** You'll be ten times more
   confident describing why the SAT solver doesn't win.
3. **Decide whether to spin up a Bitcoin node later.** Pruned mode on
   this machine is ~10 GB, takes 1–3 days to sync, and unlocks live
   `getblocktemplate` so you could (in principle) try mining a real
   block. Not necessary for the academic write-up.
4. **Try the same pipeline against SHA-1** instead of SHA-256, just for
   contrast. Reduced-round SHA-1 collisions have been found by SAT in
   the literature; you'd see meaningfully different solver behavior.
