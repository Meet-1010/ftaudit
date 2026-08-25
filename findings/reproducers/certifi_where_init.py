#!/usr/bin/env python3
"""certifi: concurrent where() enters the resource context more than once.

certifi/core.py::where lazily materialises the CA bundle:

    global _CACERT_CTX
    global _CACERT_PATH
    if _CACERT_PATH is None:
        _CACERT_CTX = as_file(files("certifi").joinpath("cacert.pem"))
        _CACERT_PATH = str(_CACERT_CTX.__enter__())
        atexit.register(exit_cacert_ctx)

Under concurrency several threads pass the `is None` test.  Each opens its own
context manager and registers its own atexit hook, but only the last assignment
to _CACERT_CTX survives, so the earlier context managers are orphaned.  When
certifi is imported from a zip/wheel the context manager extracts a temporary
file, and an orphaned one is a temp file that is never cleaned up -- while
exit_cacert_ctx runs once per extra registration.

Exit 0 = context entered once; 1 = entered more than once.
"""
import atexit
import sys
import threading

import certifi.core as core

THREADS = int(sys.argv[1]) if len(sys.argv) > 1 else 16
TRIALS = int(sys.argv[2]) if len(sys.argv) > 2 else 200

if not hasattr(core, "_CACERT_PATH"):
    print("this certifi build has no lazy _CACERT_PATH path; nothing to test")
    raise SystemExit(0)


def main() -> int:
    registrations = []
    lock = threading.Lock()
    real_register = atexit.register

    def counting_register(fn, *a, **kw):
        with lock:
            registrations.append(fn)
        return real_register(fn, *a, **kw)

    core.atexit.register = counting_register  # type: ignore[attr-defined]

    bad = []
    for trial in range(TRIALS):
        core._CACERT_PATH = None
        core._CACERT_CTX = None
        registrations.clear()

        barrier = threading.Barrier(THREADS)
        results: list[str] = [""] * THREADS

        def worker(i: int) -> None:
            barrier.wait()
            results[i] = core.where()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        n = len(registrations)
        if n != 1:
            bad.append((trial, n))

    core.atexit.register = real_register  # type: ignore[attr-defined]
    print(f"python={sys.version.split()[0]} gil_enabled={sys._is_gil_enabled()}")
    print(f"trials where the resource context was entered != 1 time: {len(bad)}/{TRIALS}")
    for trial, n in bad[:5]:
        print(f"  trial {trial}: entered {n} times -> {n - 1} orphaned context manager(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
