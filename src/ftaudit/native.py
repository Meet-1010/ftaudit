"""Static detection of native extensions that will re-enable the GIL.

An extension module only keeps free-threading alive if it explicitly declares
support:

* C, multi-phase init:  a ``{Py_mod_gil, Py_MOD_GIL_NOT_USED}`` slot
* C, single-phase init: ``PyUnstable_Module_SetGIL(m, Py_MOD_GIL_NOT_USED)``
* Cython:               ``# cython: freethreading_compatible = True``
  (or the equivalent ``compiler_directives`` entry in setup.py / pyproject)

Anything else makes CPython switch the GIL back on for the whole process at
import time.  ``ftaudit gilcheck`` catches this at runtime for installed
wheels; this module catches it in a source tree, before the wheel is built.
"""

from __future__ import annotations

import os
import re

from .model import Confidence, Finding, Severity
from .rules import RULES

_C_EXT = (".c", ".cpp", ".cc", ".cxx", ".m")
# .pxd is a *declaration* file -- a Cython header.  It never compiles to a
# module of its own, so it can neither carry nor need the freethreading
# directive; only .pyx implementation files can.
_CY_EXT = (".pyx",)

_DECLARES_C = re.compile(
    r"Py_mod_gil|PyUnstable_Module_SetGIL|Py_MOD_GIL_NOT_USED"
)
_DEFINES_MODULE = re.compile(r"PyModuleDef\b|PyModule_Create|PyModuleDef_Init|PyInit_")
_DECLARES_CYTHON = re.compile(
    r"#\s*cython\s*:.*freethreading_compatible\s*=\s*True", re.IGNORECASE
)
_CYTHON_MODULE = re.compile(r"^\s*(cdef|cpdef|def|cimport|from\s+\S+\s+cimport)\b", re.MULTILINE)

_BUILD_DECLARES = re.compile(r"freethreading_compatible['\"]?\s*[:=]\s*True")


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _build_config_declares(root: str) -> bool:
    """True when setup.py / pyproject.toml sets the Cython directive globally."""
    for name in ("setup.py", "pyproject.toml", "setup.cfg", "meson.build"):
        path = os.path.join(root, name)
        if os.path.exists(path) and _BUILD_DECLARES.search(_read(path)):
            return True
    return False


def scan_native_sources(root: str) -> tuple[list[Finding], list[str]]:
    """Find extension sources that do not declare free-threading support."""
    rule = RULES["FT112"]
    findings: list[Finding] = []
    sources: list[str] = []
    if os.path.isfile(root):
        candidates = [root]
        base = os.path.dirname(root)
    else:
        base = root
        candidates = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in {".git", "build", "dist", "__pycache__", ".tox"}]
            for fn in filenames:
                if fn.endswith(_C_EXT + _CY_EXT):
                    candidates.append(os.path.join(dirpath, fn))
                elif fn.endswith(".pxd"):
                    sources.append(os.path.relpath(os.path.join(dirpath, fn), root))

    global_declared = _build_config_declares(base)

    for path in candidates:
        text = _read(path)
        if not text:
            continue
        rel = os.path.relpath(path, base) if os.path.isdir(root) else os.path.basename(path)
        is_cython = path.endswith(_CY_EXT)

        if is_cython:
            if not _CYTHON_MODULE.search(text):
                continue
            sources.append(rel)
            if global_declared or _DECLARES_CYTHON.search(text):
                continue
            message = (
                f"Cython module `{rel}` does not set "
                "`# cython: freethreading_compatible = True`"
            )
        else:
            # Only C files that actually define a module matter.
            if not _DEFINES_MODULE.search(text):
                continue
            sources.append(rel)
            if _DECLARES_C.search(text):
                continue
            message = (
                f"C extension `{rel}` defines a module but never sets "
                "Py_mod_gil / PyUnstable_Module_SetGIL"
            )

        # point at the first line that looks like the module definition
        line = 1
        for i, l in enumerate(text.splitlines(), 1):
            if (_DEFINES_MODULE.search(l) if not is_cython else _CYTHON_MODULE.match(l)):
                line = i
                break

        findings.append(
            Finding(
                rule="FT112",
                name=rule.name,
                message=message,
                path=rel,
                line=line,
                col=0,
                end_line=line,
                severity=Severity.HIGH,
                confidence=Confidence.HIGH if not is_cython else Confidence.MEDIUM,
                symbol=rel,
                function="<module definition>",
                snippet="",
                why=rule.why,
                fix=rule.fix,
                extra={"language": "cython" if is_cython else "c"},
            )
        )
    return findings, sources
