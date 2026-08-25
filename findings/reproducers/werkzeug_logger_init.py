#!/usr/bin/env python3
"""Werkzeug: concurrent first log call installs the stream handler twice.

werkzeug/_internal.py::_log lazily creates the module logger:

    global _logger
    if _logger is None:
        _logger = logging.getLogger("werkzeug")
        if _logger.level == logging.NOTSET:
            _logger.setLevel(logging.INFO)
        if not _has_level_handler(_logger):
            _logger.addHandler(_ColorStreamHandler())

Both the `_logger is None` test and the `_has_level_handler` test are
check-then-act.  When several threads emit their first werkzeug log line at the
same moment they each observe "no handler yet" and each add one, after which
every werkzeug log line is emitted once per extra handler.

Exit 0 = exactly one handler; 1 = duplicate handlers installed.
"""
import contextlib
import io
import logging
import os
import sys
import threading

import werkzeug._internal as wi

THREADS = int(sys.argv[1]) if len(sys.argv) > 1 else 16
TRIALS = int(sys.argv[2]) if len(sys.argv) > 2 else 200


def main() -> int:
    logger = logging.getLogger("werkzeug")
    bad = []
    # The handler under test writes to stderr; send that to /dev/null so the
    # reproducer's own output stays readable.
    devnull = open(os.devnull, "w")
    real_stderr, sys.stderr = sys.stderr, devnull
    for trial in range(TRIALS):
        # reset to the pristine, never-logged-to state
        wi._logger = None
        logger.handlers.clear()
        logger.setLevel(logging.NOTSET)

        barrier = threading.Barrier(THREADS)

        def worker() -> None:
            barrier.wait()
            wi._log("info", "hello")

        threads = [threading.Thread(target=worker) for _ in range(THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        n = len(logger.handlers)
        if n != 1:
            bad.append((trial, n))

    sys.stderr = real_stderr
    devnull.close()
    logger.handlers.clear()
    print(f"python={sys.version.split()[0]} gil_enabled={sys._is_gil_enabled()}")
    print(f"trials with the wrong handler count: {len(bad)}/{TRIALS}")
    for trial, n in bad[:5]:
        print(f"  trial {trial}: {n} handlers installed (expected 1) "
              f"-> every werkzeug log line printed {n} times")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
