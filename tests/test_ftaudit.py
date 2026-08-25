"""ftaudit's own test suite.

The corpus files under tests/corpus are the contract: every rule must fire on
racy.py, and safe.py -- which contains the correct version of each pattern --
must stay clean.  That pairing is what keeps the analyser honest as rules grow.
"""

from __future__ import annotations

import itertools
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(HERE, "corpus"))

from ftaudit.model import Severity                       # noqa: E402
from ftaudit.rules import RULES                          # noqa: E402
from ftaudit.staticscan import scan_file, scan_source, scan_tree  # noqa: E402
from ftaudit.stress import (                             # noqa: E402
    ThreadStress, is_freethreaded_build, gil_enabled,
    probe_lost_update, probe_singleton,
)

RACY = os.path.join(HERE, "corpus", "racy.py")
SAFE = os.path.join(HERE, "corpus", "safe.py")

requires_ft = pytest.mark.skipif(
    not is_freethreaded_build() or gil_enabled(),
    reason="needs a free-threaded interpreter with the GIL actually disabled",
)


# --------------------------------------------------------------------------- #
# static analysis
# --------------------------------------------------------------------------- #

def test_every_rule_fires_on_the_racy_corpus():
    """The corpus is the contract: a rule with no fixture is a rule nobody tests."""
    result = scan_tree(os.path.join(HERE, "corpus"), include_tests=True)
    fired = {f.rule for f in result.findings}
    missing = set(RULES) - fired
    assert not missing, f"rules that never fired: {sorted(missing)}"


def test_native_scan_flags_undeclared_cython_only():
    from ftaudit.native import scan_native_sources

    findings, sources = scan_native_sources(os.path.join(HERE, "corpus", "native"))
    assert set(sources) == {"mymod.pyx", "safe_mod.pyx"}
    flagged = {f.path for f in findings}
    assert flagged == {"mymod.pyx"}, "the declared module must not be flagged"


def test_safe_corpus_has_no_actionable_findings():
    findings, err = scan_file(SAFE, HERE)
    assert err is None
    actionable = [f for f in findings if f.severity.rank >= Severity.LOW.rank]
    assert not actionable, "false positives:\n" + "\n".join(
        f"  {f.rule} line {f.line}: {f.message}" for f in actionable
    )


def test_findings_under_a_lock_are_downgraded():
    src = """
import threading
_LOCK = threading.Lock()
_CACHE = {}

def write(k, v):
    with _LOCK:
        _CACHE[k] = v
"""
    findings = scan_source(src, "x.py")
    assert all(f.severity == Severity.INFO for f in findings)


def test_module_level_mutation_is_not_reported():
    # Runs once at import, under the import lock: not a race.
    src = """
_TABLE = {}
for i in range(3):
    _TABLE[i] = i
"""
    assert scan_source(src, "x.py") == []


def test_correct_double_checked_locking_is_accepted():
    src = """
import threading
_LOCK = threading.Lock()
_X = None

def get():
    global _X
    if _X is None:
        with _LOCK:
            if _X is None:
                _X = object()
    return _X
"""
    actionable = [f for f in scan_source(src, "x.py") if f.severity.rank >= Severity.LOW.rank]
    assert not actionable


def test_broken_double_checked_locking_is_reported():
    src = """
import threading
_LOCK = threading.Lock()
_X = None

def get():
    global _X
    if _X is None:
        with _LOCK:
            _X = object()
    return _X
"""
    assert any(f.rule == "FT108" for f in scan_source(src, "x.py"))


def test_setdefault_is_treated_as_atomic():
    src = """
_CACHE = {}

def get(k):
    return _CACHE.setdefault(k, [])
"""
    actionable = [f for f in scan_source(src, "x.py") if f.severity.rank >= Severity.MEDIUM.rank]
    assert not actionable


def test_suppression_comment_is_honoured():
    src = """
_N = 0

def bump():
    global _N
    _N += 1  # noqa: FT105
"""
    assert not [f for f in scan_source(src, "x.py") if f.rule == "FT105"]


@pytest.mark.parametrize("rule_id", sorted(RULES))
def test_every_rule_has_prose(rule_id):
    rule = RULES[rule_id]
    assert rule.why.strip() and rule.fix.strip()
    assert rule.summary.strip()


def test_scanner_survives_syntax_errors(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("def (:\n")
    findings, err = scan_file(str(bad), str(tmp_path))
    assert findings == [] and err and "SyntaxError" in err


# --------------------------------------------------------------------------- #
# runtime probes
# --------------------------------------------------------------------------- #

@requires_ft
def test_probe_detects_lost_updates():
    import racy

    result = probe_lost_update(
        racy.bump, lambda: racy._COUNTER, lambda: setattr(racy, "_COUNTER", 0),
        iterations=3000, repeats=3,
    )
    assert not result.ok, "expected the counter race to be detected"
    assert result.violations


@requires_ft
def test_probe_detects_duplicate_singleton_init():
    import racy

    # A one-bytecode window needs oversubscription to be observed reliably.
    result = probe_singleton(
        racy.get_instance, lambda: setattr(racy, "_INSTANCE", None),
        repeats=150, escalating=True,
    )
    assert not result.ok, f"expected the lazy-init race to be detected ({result.summary()})"


@requires_ft
def test_probe_reports_clean_for_correctly_locked_code():
    import safe

    result = probe_singleton(
        safe.correct_dcl, lambda: setattr(safe, "_INSTANCE", None),
        repeats=150, escalating=True,
    )
    assert result.ok, f"false positive: {result.violations}"


def test_stress_harness_collects_exceptions():
    def boom(t, i):
        raise RuntimeError("nope")

    result = ThreadStress(threads=4, iterations=2, repeats=1, label="t").run(boom)
    assert not result.ok
    assert result.failures and result.failures[0].exc_type == "RuntimeError"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "ftaudit.cli", *args],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": os.path.join(ROOT, "src")},
    )


def test_cli_scan_reports_and_sets_exit_status():
    p = _cli("scan", RACY, "--no-snippets")
    assert p.returncode == 1          # high-severity findings present
    assert "FT102" in p.stdout


def test_cli_scan_json_is_valid():
    import json
    p = _cli("scan", RACY, "--json")
    data = json.loads(p.stdout)
    assert data["summary"]["by_severity"]["high"] > 0


def test_cli_scan_clean_target_exits_zero():
    p = _cli("scan", SAFE, "--no-snippets")
    assert p.returncode == 0


def test_cli_rules_lists_catalogue():
    p = _cli("rules")
    assert p.returncode == 0
    for rule_id in RULES:
        assert rule_id in p.stdout
