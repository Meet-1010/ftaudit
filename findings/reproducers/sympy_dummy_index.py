#!/usr/bin/env python3
"""SymPy: concurrent Dummy() creation hands out colliding dummy_index values.

sympy/core/symbol.py::Dummy.__new__ does:

    if dummy_index is None:
        dummy_index = Dummy._base_dummy_index + Dummy._count   # read
        Dummy._count += 1                                       # increment

The read and the increment are separate operations on shared class state, so
two threads can read the same _count and receive the same dummy_index.

That matters more than a duplicated integer, because line ~521 puts the index
into the symbol's identity:

    def _hashable_content(self):
        return Symbol._hashable_content(self) + (self.dummy_index,)

Two Dummy objects that collide on dummy_index therefore compare *equal* and
hash identically. Dummy symbols exist precisely so that generated variables are
distinct from each other; a collision silently breaks substitution, integration
and solving, producing a wrong answer rather than an error.

Exit 0 = every Dummy was unique; 1 = collisions observed.
"""

import sys
import threading

from sympy import Dummy

THREADS = int(sys.argv[1]) if len(sys.argv) > 1 else 16
PER_THREAD = int(sys.argv[2]) if len(sys.argv) > 2 else 400


def main() -> int:
    barrier = threading.Barrier(THREADS)
    buckets: list[list] = [[] for _ in range(THREADS)]

    def worker(i: int) -> None:
        barrier.wait()
        out = buckets[i]
        for _ in range(PER_THREAD):
            out.append(Dummy())

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    dummies = [d for b in buckets for d in b]
    indices = [d.dummy_index for d in dummies]
    total = len(indices)
    unique_idx = len(set(indices))

    # the consequence: distinct Dummy objects that compare equal
    equal_pairs = total - len({(d.name, d.dummy_index) for d in dummies})

    print(f"python={sys.version.split()[0]} gil_enabled={sys._is_gil_enabled()}")
    print(f"created {total} Dummy symbols across {THREADS} threads")
    print(f"  distinct dummy_index values : {unique_idx}")
    print(f"  colliding indices           : {total - unique_idx}")
    print(f"  Dummy pairs now comparing equal: {equal_pairs}")

    if total != unique_idx:
        dupes = {}
        for d in dummies:
            dupes.setdefault(d.dummy_index, []).append(d)
        sample = [(k, [str(x) for x in v]) for k, v in dupes.items() if len(v) > 1][:3]
        for idx, names in sample:
            print(f"    index {idx} shared by {len(names)} symbols: {names}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
