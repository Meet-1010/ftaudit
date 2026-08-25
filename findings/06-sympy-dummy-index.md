# SymPy: concurrent `Dummy()` hands out colliding indices, silently corrupting results

**Status:** patch + regression test ready ([`patches/sympy-dummy-index-thread-safe.patch`](patches/sympy-dummy-index-thread-safe.patch))
**Severity:** high — silently wrong mathematical results, no exception
**Upstream home:** [sympy/sympy#28239 "SymPy free threading support"](https://github.com/sympy/sympy/issues/28239) (open)

## The bug

`sympy/core/symbol.py::Dummy.__new__`:

```python
if name is None:
    name = "Dummy_" + str(Dummy._count)

if dummy_index is None:
    dummy_index = Dummy._base_dummy_index + Dummy._count   # read
    Dummy._count += 1                                       # increment
```

The read and the increment are separate operations on shared class state, so two
threads can be handed the same `_count` and therefore the same `dummy_index`.

That is not merely a duplicated integer. `dummy_index` is part of the symbol's
identity:

```python
def _hashable_content(self):
    return Symbol._hashable_content(self) + (self.dummy_index,)
```

Two `Dummy` objects that collide on the index therefore **compare equal and hash
identically**. Dummy symbols exist precisely so that generated variables are
guaranteed distinct; a collision defeats that guarantee everywhere SymPy relies
on it — substitution, integration, `solve`, `simplify`.

## Evidence

[`reproducers/sympy_dummy_index.py`](reproducers/sympy_dummy_index.py) — 16
threads each creating 400 dummies, SymPy 1.14.0 on free-threaded 3.14.4:

```console
$ python3.14t reproducers/sympy_dummy_index.py
python=3.14.4 gil_enabled=False
created 6400 Dummy symbols across 16 threads
  distinct dummy_index values : 1720
  colliding indices           : 4680
  Dummy pairs now comparing equal: 4319
    index 7538347 shared by 6 symbols: ['_Dummy_168', '_Dummy_261', ...]

$ PYTHON_GIL=1 python3.14t reproducers/sympy_dummy_index.py
created 6400 Dummy symbols across 16 threads
  distinct dummy_index values : 6400
  colliding indices           : 0
```

**73% of the symbols collided.** Same interpreter, same wheel; the GIL is the
only variable.

### The consequence is a wrong answer, not a crash

Each of 16 threads asks for one private `Dummy`, then one thread substitutes into
its own expression:

```
thread 0 built:      _Dummy_46**2 + 1
thread 3 substitutes its own dummy -> 0:
  expr.subs(_Dummy_46, 0) = 1        <-- should be unchanged
```

Thread 3's substitution silently rewrote thread 0's expression, because the two
"distinct" dummies compare equal. No exception is raised; the caller simply gets
the wrong result.

## Fix

Take the counter value under a `threading.Lock`, and derive both the generated
name and the index from that single value:

```python
if dummy_index is None:
    with Dummy._count_lock:
        count = Dummy._count
        Dummy._count = count + 1
    if name is None:
        name = "Dummy_" + str(count)
    dummy_index = Dummy._base_dummy_index + count
```

Restructuring into the `dummy_index is None` branch is behaviour-preserving: the
`assert` at the top of `__new__` already guarantees that a caller supplying
`dummy_index` also supplies `name`, so `name is None` implies `dummy_index is
None`. It also fixes a second latent bug — previously the name and the index each
read `_count` separately and could be drawn from different values.

`Dummy._count` is kept as a class attribute, so the existing
`test_symbol.py` assertion that reads it still passes.

## Validation

| Check | Result |
| --- | --- |
| `sympy/core/tests/` | **1983 passed**, 73 skipped, 23 xfailed |
| core + integrals + solvers + simplify | **2617 passed**, 73 skipped, 45 xfailed |
| New regression test **without** the patch | fails (1059 distinct indices out of 2000) |
| New regression test **with** the patch | passes |
| Reproducer after patch | 0 collisions in 6400 symbols |
| `Dummy()` cost, single-threaded | 818.2 ns → 825.4 ns (**+0.9%**) |

The 0.9% figure matters because `Dummy()` is hot — integration and solving create
many dummies. A lock acquire is ~7 ns against an ~818 ns construction that already
runs `_sanitize`, `Symbol.__xnew__` and the assumptions machinery.

## Fit with upstream

[#28239](https://github.com/sympy/sympy/issues/28239) is the open free-threading
tracking issue. SymPy has already merged the same class of fix twice —
[#28768](https://github.com/sympy/sympy/pull/28768) added an internal `RLock` to
`Sieve`, and [#28769](https://github.com/sympy/sympy/pull/28769) made the
`ntheory` functions thread-safe — so a lock around shared counter state is
established precedent rather than a new direction. `test_symbol.py` already
imports `threading` and carries a threading regression test (for #16734), so the
new test sits in the existing pattern.

## Caveats

- The severity depends on actually creating dummies from several threads. Code
  that does all its symbolic work on one thread is unaffected.
- I have not audited whether other SymPy global counters share this shape;
  `_count` is referenced in only four places in `symbol.py`, but the codebase has
  other module-level caches that the scan flagged separately.
