<!-- Draft PR for https://github.com/sympy/sympy  -->
<!-- Branch suggestion: dummy-index-thread-safe -->
<!-- SymPy requires the release-notes block below; keep it. -->

# TITLE

core: make Dummy index allocation thread-safe

# BODY

References #28239.

#### Brief description of what is fixed or changed

`Dummy.__new__` read `Dummy._count` and incremented it as two separate
operations:

```python
if name is None:
    name = "Dummy_" + str(Dummy._count)

if dummy_index is None:
    dummy_index = Dummy._base_dummy_index + Dummy._count   # read
    Dummy._count += 1                                       # increment
```

Concurrent `Dummy()` calls can therefore be handed the same count and the same
`dummy_index`. Because `dummy_index` is part of `_hashable_content`, two Dummy
objects that collide on it **compare equal and hash identically** — which defeats
the entire purpose of a Dummy and silently corrupts anything that relies on
generated symbols being distinct.

On a free-threaded 3.14.4 build, 16 threads creating 400 dummies each:

```
created 6400 Dummy symbols across 16 threads
  distinct dummy_index values : 1720
  colliding indices           : 4680
  Dummy pairs now comparing equal: 4319
```

73% collided. With `PYTHON_GIL=1` on the same interpreter: 0 collisions.

The consequence is a wrong answer rather than an error. Sixteen threads each ask
for one private Dummy, then one thread substitutes into its own expression:

```
thread 0 built:      _Dummy_46**2 + 1
thread 3 substitutes its own dummy -> 0:
  expr.subs(_Dummy_46, 0) = 1        <-- should be unchanged
```

This takes the counter value under a lock and derives both the generated name and
the index from that single value. Moving both into the `dummy_index is None`
branch is behaviour-preserving — the `assert` at the top of `__new__` already
guarantees that a caller supplying `dummy_index` also supplies `name`, so
`name is None` implies `dummy_index is None`. It also closes a second latent bug:
previously the name and the index each read `_count` separately and could be drawn
from different values.

`Dummy._count` stays a class attribute, so the existing assertion in
`test_symbol.py` that reads it is unaffected.

#### Performance

`Dummy()` is hot, so the lock cost is the obvious objection. Measured
single-threaded, 20000 constructions, best of 5:

| | ns per `Dummy()` |
| --- | --- |
| before | 818.2 |
| after | 825.4 |
| delta | **+7.1 ns (+0.9%)** |

A lock acquire is ~7 ns against an ~818 ns construction that already runs
`_sanitize`, `Symbol.__xnew__` and the assumptions machinery.

If you would rather avoid the lock entirely, `itertools.count()` gives an atomic
ticket with no lock at all — but it cannot keep `Dummy._count` in sync as a plain
readable attribute, which would change `test_symbol.py:60`. I went with the lock
as the smaller behavioural change; happy to switch if you prefer the lock-free
version and are fine adjusting that assertion.

#### Testing

`test_Dummy_index_is_unique_across_threads` in `sympy/core/tests/test_symbol.py`,
following the existing threading test in that file. It fails on master
(1059 distinct indices out of 2000) and passes with this change.

| | |
| --- | --- |
| `sympy/core/tests/` | 1983 passed, 73 skipped, 23 xfailed |
| core + integrals + solvers + simplify | 2617 passed, 73 skipped, 45 xfailed |

Environment: CPython 3.14.4 free-threaded, macOS 15 / arm64.

#### Other comments

This is the same class of fix as #28768 (internal `RLock` in `Sieve`) and #28769
(thread-safe `ntheory` functions), both merged under #28239.

#### Release Notes

<!-- BEGIN RELEASE NOTES -->
* core
  * Fixed a race in `Dummy` where concurrently created dummy symbols could be
    given the same `dummy_index` and therefore compare equal to one another.
<!-- END RELEASE NOTES -->
