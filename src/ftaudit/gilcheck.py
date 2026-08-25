"""Which installed packages silently switch the GIL back on?

A C extension that does not declare ``Py_mod_gil = Py_MOD_GIL_NOT_USED`` makes
CPython re-enable the GIL for the *entire process* the moment it is imported.
One such dependency anywhere in the tree removes free-threading from every
other library in the program, and nothing warns you loudly.

This module imports each candidate in a fresh subprocess and reports whether
``sys._is_gil_enabled()`` flipped, which turns an invisible whole-program
regression into a per-package yes/no.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field

_PROBE = r"""
import json, sys, importlib, warnings, traceback
warnings.simplefilter("ignore")
name = sys.argv[1]
before = sys._is_gil_enabled()
out = {"module": name, "gil_before": before}
try:
    importlib.import_module(name)
except BaseException as exc:
    out.update(ok=False, error=f"{type(exc).__name__}: {exc}"[:300],
               gil_after=sys._is_gil_enabled())
else:
    out.update(ok=True, error=None, gil_after=sys._is_gil_enabled())
print("FTAUDIT_JSON:" + json.dumps(out))
"""


@dataclass
class GilReport:
    module: str
    ok: bool
    reenabled_gil: bool
    error: str | None = None
    crashed: bool = False
    returncode: int = 0
    stderr: str = ""

    @property
    def status(self) -> str:
        if self.crashed:
            return "CRASH"
        if not self.ok:
            return "IMPORT-FAIL"
        return "GIL-REENABLED" if self.reenabled_gil else "ok"


@dataclass
class GilScan:
    interpreter: str
    reports: list[GilReport] = field(default_factory=list)

    def offenders(self) -> list[GilReport]:
        return [r for r in self.reports if r.reenabled_gil]

    def crashed(self) -> list[GilReport]:
        return [r for r in self.reports if r.crashed]

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(
            {
                "interpreter": self.interpreter,
                "summary": {
                    "checked": len(self.reports),
                    "gil_reenabled": len(self.offenders()),
                    "crashed": len(self.crashed()),
                    "import_failed": sum(1 for r in self.reports if not r.ok and not r.crashed),
                },
                "results": [
                    {
                        "module": r.module,
                        "status": r.status,
                        "reenabled_gil": r.reenabled_gil,
                        "error": r.error,
                        "returncode": r.returncode,
                    }
                    for r in sorted(self.reports, key=lambda r: (not r.reenabled_gil, not r.crashed, r.module))
                ],
            },
            indent=indent,
        )


def check_module(name: str, python: str | None = None, timeout: float = 120.0) -> GilReport:
    """Import `name` in a fresh interpreter and see whether the GIL came back."""
    exe = python or sys.executable
    try:
        proc = subprocess.run(
            [exe, "-c", _PROBE, name],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return GilReport(name, ok=False, reenabled_gil=False, error="import timed out", crashed=False, returncode=-1)

    payload = None
    for line in proc.stdout.splitlines():
        if line.startswith("FTAUDIT_JSON:"):
            payload = json.loads(line[len("FTAUDIT_JSON:"):])
            break

    if payload is None:
        return GilReport(
            name,
            ok=False,
            reenabled_gil=False,
            error="interpreter died before reporting",
            crashed=True,
            returncode=proc.returncode,
            stderr=(proc.stderr or "")[-800:],
        )

    return GilReport(
        module=name,
        ok=bool(payload["ok"]),
        reenabled_gil=bool(payload["gil_after"]) and not bool(payload["gil_before"]),
        error=payload.get("error"),
        crashed=False,
        returncode=proc.returncode,
        stderr=(proc.stderr or "")[-400:],
    )


def installed_top_level_modules(python: str | None = None) -> list[str]:
    """Best-effort list of importable top-level names from installed distributions."""
    exe = python or sys.executable
    code = r"""
import json, sys
from importlib import metadata
names = set()
for dist in metadata.distributions():
    try:
        tops = dist.read_text("top_level.txt")
    except Exception:
        tops = None
    if tops:
        for line in tops.splitlines():
            line = line.strip()
            if line and not line.startswith("_") and "/" not in line:
                names.add(line)
        continue
    meta_name = (dist.metadata["Name"] or "").replace("-", "_")
    if meta_name:
        names.add(meta_name)
print(json.dumps(sorted(names)))
"""
    proc = subprocess.run([exe, "-c", code], capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        return []
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return []


def scan(modules: list[str], python: str | None = None, timeout: float = 120.0) -> GilScan:
    exe = python or sys.executable
    tag = subprocess.run(
        [exe, "-c", "import sys,sysconfig;print(sys.version.split()[0], bool(sysconfig.get_config_var('Py_GIL_DISABLED')), sys._is_gil_enabled())"],
        capture_output=True, text=True,
    ).stdout.strip()
    result = GilScan(interpreter=f"{exe} :: {tag}")
    for name in modules:
        result.reports.append(check_module(name, python=exe, timeout=timeout))
    return result
