#!/usr/bin/env python3
"""Minimal reproducer: concurrent iteration of one generator crashes the
free-threaded interpreter on CPython 3.13.x and 3.14.x.

Expected behaviour
------------------
CPython documents that a generator cannot be re-entered.  A second thread that
calls next() while the generator is running must observe

    ValueError: generator already executing

Observed behaviour on a free-threaded 3.13/3.14 build
-----------------------------------------------------
The process dies with SIGSEGV (rc -11), SIGBUS (rc -10), or

    Fatal Python error: _TAIL_CALL_CACHE: Executing a cache.

on builds configured --with-tail-call-interp.  No Python-level exception is
raised, so the crash is not catchable and takes the whole process down.

Usage
-----
    python3.14t gen_concurrent_next.py [threads] [iterations] [kind]

kind is one of: trivial (default), counter, range.
Exit status 0 means the run survived; any non-zero status is a crash.
"""

from __future__ import annotations

import sys
import threading

THREADS = int(sys.argv[1]) if len(sys.argv) > 1 else 2
ITERATIONS = int(sys.argv[2]) if len(sys.argv) > 2 else 3000
KIND = sys.argv[3] if len(sys.argv) > 3 else "trivial"


def make_generator(kind: str):
    if kind == "trivial":
        def gen():
            while True:
                yield 1
    elif kind == "counter":
        def gen():
            n = 0
            while True:
                yield n
                n += 1
    elif kind == "range":
        def gen():
            yield from range(10 ** 9)
    else:
        raise SystemExit(f"unknown kind: {kind}")
    return gen()


def main() -> int:
    shared = make_generator(KIND)
    barrier = threading.Barrier(THREADS)

    def worker() -> None:
        barrier.wait()
        for _ in range(ITERATIONS):
            try:
                next(shared)
            except ValueError:
                pass          # the documented, correct outcome
            except StopIteration:
                return

    threads = [threading.Thread(target=worker) for _ in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    print(
        f"survived  python={sys.version.split()[0]}  "
        f"gil_enabled={sys._is_gil_enabled()}  "
        f"threads={THREADS} iterations={ITERATIONS} kind={KIND}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
