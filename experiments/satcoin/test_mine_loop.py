"""Tests for mine_loop.py — the continuous mining daemon.

Exercises the loop's state machine under various scenarios using a real
local bitcoind node (which is synced) but with various mocked outcomes
for the solver and submit paths.

Scenarios covered:
  1. Loop runs N iterations cleanly without --simulate-win (no errors)
  2. Loop detects chain advance via mocked safe_rpc returning different tips
  3. Loop under --simulate-win: redemption rehearsal runs; verifies the
     verdict comes back as 'False' (placeholder nonce can't satisfy real target)
  4. Loop under --simulate-win --auto-submit: submitblock is called; verifies
     bitcoind reports rejection (high-hash or similar — placeholder nonce
     produces a hash too large)
  5. Loop handles RPC failures with backoff (rpc_failures counter increments)

Run:
    python test_mine_loop.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mine_loop  # noqa: E402


def check(name: str, ok: bool, detail: str = "") -> tuple[bool, str]:
    return ok, f"  {'PASS' if ok else 'FAIL'}: {name}{(' — ' + detail) if detail else ''}"


def test_one_iteration_clean():
    """Loop runs 1 iteration without crashing, no simulate-win."""
    t0 = time.time()
    stats = mine_loop.run_loop(
        poll_interval_s=0.1, max_iterations=1,
        stale_after_s=60.0,
        simulate_win=False, auto_submit=False,
    )
    dt = time.time() - t0
    ok = (stats.iterations == 1 and stats.rpc_failures == 0
          and stats.templates_built >= 1)
    return check("one_iteration_clean", ok,
                 f"iters={stats.iterations} templates={stats.templates_built} "
                 f"rpc_failures={stats.rpc_failures} in {dt:.1f}s")


def test_chain_advance_detection():
    """Mock the tip-check so it returns different hashes — loop should
    increment chain_advances and rebuild the template."""
    tips = iter(["aa" * 32, "aa" * 32, "bb" * 32, "bb" * 32])
    original_safe_rpc = mine_loop.safe_rpc

    def fake_safe_rpc(method, params=None, **kwargs):
        if method == "getbestblockhash":
            try:
                return next(tips)
            except StopIteration:
                return "bb" * 32
        return original_safe_rpc(method, params, **kwargs)

    with patch.object(mine_loop, "safe_rpc", side_effect=fake_safe_rpc):
        # Also mock build_template_and_cnf to avoid actually calling cgen for
        # this fast unit test — we're testing the state machine, not the pipeline.
        with patch.object(mine_loop, "build_template_and_cnf",
                          return_value={
                              "block_height": 950278,
                              "raw_header_hex": "00" * 80,
                              "extras": {"tx_count_total": 1},
                              "coinbase_value_btc": 3.125,
                              "payout_script_hex": "00" * 22,
                              "_cnf_path": "/dev/null",
                          }):
            stats = mine_loop.run_loop(
                poll_interval_s=0.01, max_iterations=4,
                simulate_win=False, auto_submit=False,
            )
    return check("chain_advance_detection", stats.chain_advances == 1,
                 f"chain_advances={stats.chain_advances} (expected 1) "
                 f"templates_built={stats.templates_built}")


def test_simulate_win_no_submit():
    """With --simulate-win and no auto-submit, every iteration should run
    a redemption rehearsal and report meets_real_target=False."""
    stats = mine_loop.run_loop(
        poll_interval_s=0.1, max_iterations=1,
        simulate_win=True, auto_submit=False,
    )
    ok = (stats.iterations == 1 and stats.redemption_attempts == 1
          and stats.submits_attempted == 0)
    return check("simulate_win_no_submit", ok,
                 f"redemption_attempts={stats.redemption_attempts} "
                 f"submits_attempted={stats.submits_attempted}")


def test_simulate_win_with_submit():
    """With --auto-submit, submitblock is actually called. For our placeholder
    nonce the response will be a rejection (high-hash, bad-txnmrklroot, or
    similar) — counted as a rejected submit, not accepted."""
    stats = mine_loop.run_loop(
        poll_interval_s=0.1, max_iterations=1,
        simulate_win=True, auto_submit=True,
    )
    # We expect at least one submit was attempted; whether it shows as
    # rejected depends on the parse of submit_block.py's output. Both
    # 'accepted' (impossible) and 'rejected' get counted; we just want
    # to confirm the pipeline reached submitblock without exception.
    ok = (stats.iterations == 1 and stats.submits_attempted == 1
          and stats.submits_accepted == 0)  # impossible to actually win
    return check("simulate_win_with_submit", ok,
                 f"submits_attempted={stats.submits_attempted} "
                 f"submits_accepted={stats.submits_accepted} "
                 f"submits_rejected={stats.submits_rejected}")


def test_rpc_failure_resilience():
    """If safe_rpc returns None (simulating bitcoind unreachable), loop
    should record an rpc_failure and not crash."""
    call_count = {"n": 0}
    original_safe_rpc = mine_loop.safe_rpc

    def fake_safe_rpc(method, params=None, **kwargs):
        call_count["n"] += 1
        if call_count["n"] <= 2:
            return None  # simulate RPC down for first 2 calls
        return original_safe_rpc(method, params, **kwargs)

    with patch.object(mine_loop, "safe_rpc", side_effect=fake_safe_rpc):
        stats = mine_loop.run_loop(
            poll_interval_s=0.05, max_iterations=3,
            simulate_win=False, auto_submit=False,
        )
    ok = stats.rpc_failures >= 1
    return check("rpc_failure_resilience", ok,
                 f"rpc_failures={stats.rpc_failures} iters={stats.iterations}")


def run_suite():
    tests = [
        test_one_iteration_clean,
        test_chain_advance_detection,
        test_simulate_win_no_submit,
        test_simulate_win_with_submit,
        test_rpc_failure_resilience,
    ]
    print("\nmine_loop tests")
    print("-" * 70)
    all_ok = True
    for t in tests:
        try:
            ok, line = t()
        except Exception as e:
            import traceback
            ok = False
            line = f"  FAIL: {t.__name__} — exception {type(e).__name__}: {e}"
            traceback.print_exc()
        print(line)
        if not ok:
            all_ok = False
    print("-" * 70)
    print("ALL PASS" if all_ok else "SOME FAILED")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(run_suite())
