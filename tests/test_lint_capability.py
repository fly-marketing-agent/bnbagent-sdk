"""Tests for tools/lint_capability.py — the minimum capability-safety lint.

Verifies (a) clean current codebase passes, (b) a deliberately violating
fixture triggers the lint, (c) the ``# capability-ok:`` bypass marker
exempts a single function.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

LINT_SCRIPT = Path(__file__).parent.parent / "tools" / "lint_capability.py"


def _run_lint(*paths: str | Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(LINT_SCRIPT), *[str(p) for p in paths]],
        capture_output=True, text=True,
    )


def test_current_codebase_is_clean():
    """The shipped SDK has no @tool functions referencing WalletProvider."""
    res = _run_lint(Path(__file__).parent.parent / "bnbagent")
    assert res.returncode == 0, f"stderr: {res.stderr}"
    assert "clean" in res.stdout


def test_violation_fixture_triggers_lint(tmp_path):
    """A @tool function that captures a WalletProvider in its closure
    should be flagged."""
    bad = tmp_path / "bad_tool.py"
    bad.write_text(textwrap.dedent("""
        from bnbagent import EVMWalletProvider, WalletProvider


        wallet: WalletProvider = EVMWalletProvider(password="x")


        def tool(fn):  # local @tool stub mimicking ADK
            return fn


        @tool
        def pay_for_resource(url: str) -> str:
            # captures wallet from outer scope — VIOLATION
            return wallet.sign_message(url)["signature"]
    """))
    res = _run_lint(bad)
    assert res.returncode == 1, f"expected violation; stdout={res.stdout!r}; stderr={res.stderr!r}"
    assert "pay_for_resource" in res.stderr
    assert ("WalletProvider" in res.stderr or "wallet" in res.stderr)


def test_violation_in_argument_annotation_triggers(tmp_path):
    bad = tmp_path / "bad_tool_arg.py"
    bad.write_text(textwrap.dedent("""
        from bnbagent import WalletProvider


        def tool(fn): return fn


        @tool
        def do_pay(wallet: WalletProvider, url: str) -> str:
            return wallet.sign_message(url)["signature"]
    """))
    res = _run_lint(bad)
    assert res.returncode == 1
    assert "do_pay" in res.stderr
    assert "WalletProvider" in res.stderr


def test_bypass_marker_exempts_function(tmp_path):
    """A def line carrying `# capability-ok: <reason>` must be skipped."""
    bypassed = tmp_path / "ok_tool.py"
    bypassed.write_text(textwrap.dedent("""
        from bnbagent import EVMWalletProvider, WalletProvider


        wallet: WalletProvider = EVMWalletProvider(password="x")


        def tool(fn): return fn


        @tool
        def migration_only(url: str) -> str:  # capability-ok: one-off migration tool
            return wallet.sign_message(url)["signature"]
    """))
    res = _run_lint(bypassed)
    assert res.returncode == 0, f"stderr: {res.stderr}"


def test_clean_tool_function_passes(tmp_path):
    """A @tool function that only uses a scoped signer (X402Signer) is fine."""
    good = tmp_path / "good_tool.py"
    good.write_text(textwrap.dedent("""
        from bnbagent import X402Signer


        signer = None  # injected at agent build time


        def tool(fn): return fn


        @tool
        def buy_resource(url: str, max_usd: float) -> str:
            assert signer is not None
            # uses signer (scoped), never WalletProvider directly
            return "<signed>"
    """))
    res = _run_lint(good)
    assert res.returncode == 0, f"stderr: {res.stderr}"
