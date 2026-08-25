"""ftaudit -- find the thread-safety bugs the GIL used to hide.

Three passes, each answering a different question:

* ``ftaudit scan``     -- static: which code *looks* racy under PEP 703?
* ``ftaudit gilcheck`` -- runtime: which dependency silently turns the GIL back on?
* ``ftaudit stress``   -- runtime: does this reproducer actually race, and is the
  race specific to free-threading (same interpreter, ``PYTHON_GIL=1``)?

Zero third-party dependencies, so it runs on a bare free-threaded interpreter
where most of the ecosystem does not yet have wheels.
"""

__version__ = "0.1.0"

from .model import Confidence, Finding, ScanResult, Severity
from .rules import RULES, Rule
from .staticscan import scan_file, scan_source, scan_tree
from .stress import ThreadStress, gil_enabled, interpreter_tag, is_freethreaded_build

__all__ = [
    "__version__",
    "Confidence",
    "Finding",
    "ScanResult",
    "Severity",
    "RULES",
    "Rule",
    "scan_file",
    "scan_source",
    "scan_tree",
    "ThreadStress",
    "gil_enabled",
    "interpreter_tag",
    "is_freethreaded_build",
]
