# CPython: concurrent generator iteration segfaults on free-threaded 3.13 / 3.14

**Status:** ready to file as a backport request against `python/cpython`
**Severity:** interpreter crash (SIGSEGV / SIGBUS) reachable from pure Python
**Affected:** every free-threaded 3.13.x and 3.14.x build
**Already fixed in:** `main` (3.15) — never backported

---

## Summary

Iterating a single generator from two or more threads hard-crashes the
free-threaded interpreter on **CPython 3.13.x and 3.14.x**. The process dies
with SIGSEGV, SIGBUS, or `Fatal Python error: _TAIL_CALL_CACHE: Executing a
cache.` — there is no Python-level exception, so the failure is not catchable
and takes the whole process down.

The documented, correct behaviour is `ValueError: generator already executing`.

The fix series for [gh-120321](https://github.com/python/cpython/issues/120321)
("SIGSEGV with generators in free-threaded build") landed on `main` between
December 2025 and August 2026 and the issue was closed **2026-08-19**. Every PR
in the series is labelled `3.15` only. **Nothing was backported to 3.14 or
3.13**, even though 3.14 is the release in which PEP 779 made free-threading
officially supported.

## Reproducer

[`reproducers/gen_concurrent_next.py`](reproducers/gen_concurrent_next.py) —
two threads, a `while True: yield 1` generator, no third-party code:

```python
import threading

def gen():
    while True:
        yield 1

g = gen()
barrier = threading.Barrier(2)

def worker():
    barrier.wait()
    for _ in range(3000):
        try:
            next(g)
        except ValueError:
            pass          # the documented, correct outcome

ts = [threading.Thread(target=worker) for _ in range(2)]
for t in ts: t.start()
for t in ts: t.join()
print("survived")
```

## Reproduction matrix

20 runs per cell, macOS 15 / arm64 (Apple M5, 10 cores), interpreters from
`uv python install`. Full data in [`crash_matrix.json`](crash_matrix.json).

| Interpreter | 2 threads × 3 000 | 8 × 20 000 | 16 × 50 000 (`yield from`) |
| --- | --- | --- | --- |
| 3.13.13 free-threaded | 2/20 | 20/20 | 18/20 |
| 3.14.0 free-threaded | 19/20 | 20/20 | 18/20 |
| 3.14.4 free-threaded | **20/20** | **20/20** | 13/20 |
| 3.15.0a8 free-threaded | 0/20 | 0/20 | 0/20 |
| 3.14.4 free-threaded, `PYTHON_GIL=1` | 0/20 | 0/20 | 0/20 |
| 3.14.7, GIL build | 0/20 | 0/20 | 0/20 |

**150 crashes in 180 runs** on the affected builds; **0 in 180** on 3.15, on the
same interpreter with the GIL forced on, and on a stock GIL build.

Observed signatures: `rc=-11` (SIGSEGV), `rc=-10` (SIGBUS),
`Fatal Python error: _TAIL_CALL_CACHE: Executing a cache.` (on builds
configured `--with-tail-call-interp`), and once
`Fatal Python error: _PyEval_EvalFrameDefault`. The tail-call interpreter only
changes how the corruption surfaces — 3.13.13t is built *without* it and still
segfaults.

## Why the fix is missing

`Objects/genobject.c` on `main` contains **38** `FT_ATOMIC_*` operations
guarding generator frame-state transitions. The `3.14` and `3.13` branches
contain **zero**:

```console
$ gh api repos/python/cpython/contents/Objects/genobject.c?ref=main | ... | grep -c FT_ATOMIC
38
$ gh api repos/python/cpython/contents/Objects/genobject.c?ref=3.14 | ... | grep -c FT_ATOMIC
0
```

The relevant PRs, all merged to `main` only:

| PR | Merged | Title |
| --- | --- | --- |
| [#142599](https://github.com/python/cpython/pull/142599) | 2025-12-19 | Make `gi_frame_state` transitions atomic in FT build |
| [#142995](https://github.com/python/cpython/pull/142995) | 2025-12-19 | Fix TSan reported race in `gen_clear_frame` |
| [#143112](https://github.com/python/cpython/pull/143112) | 2026-01-08 | Make `gen.gi_frame.clear()` thread-safe |
| [#143128](https://github.com/python/cpython/pull/143128) | 2025-12-24 | Fix TSan reported races on `gi_frame_state` |
| [#144291](https://github.com/python/cpython/pull/144291) | 2026-01-27 | Add missing `return false` in `gen_try_set_executing` |
| [#144292](https://github.com/python/cpython/pull/144292) | 2026-01-30 | Make `gi_yieldfrom` thread-safe in free-threading build |
| [#144409](https://github.com/python/cpython/pull/144409) | 2026-02-03 | Add `gi_state`, `cr_state`, `ag_state` attributes |
| [#155025](https://github.com/python/cpython/pull/155025) | 2026-08-19 | Fix thread safety of concurrently iterating async generators |

`gen_try_set_executing` is precisely the guard that is supposed to raise
`ValueError: generator already executing`; PR #144291 fixes a missing
`return false` in it.

## Why this matters for 3.14 specifically

PEP 779 removed the "experimental" label from free-threading in 3.14, which is
the signal for libraries to start shipping `cp314t` wheels and for users to run
real workloads on it. Sharing one generator across a thread pool is an ordinary
thing to do — `itertools` pipelines, lazy readers, and any
`ThreadPoolExecutor.map` over a generator can reach it — and the failure mode is
a silent process death rather than a catchable exception.

## Ask

Backport the gh-120321 series to `3.14` (and `3.13` if the branch still takes
crash fixes). If a full backport is too invasive for a stable branch, a minimal
alternative would be making the `gi_frame_state` read/write in
`gen_send_ex2` atomic so the re-entrancy guard reliably raises `ValueError`
instead of corrupting the frame.

## Caveats

- Reproduced on a single platform (macOS 15 / arm64, Apple M5). The crash is a
  data race, so timing on other platforms will differ; the 3.13.13t 2-thread
  cell (2/20) shows sensitivity to load even here.
- The interpreters are the `python-build-standalone` builds that `uv` ships,
  not builds from source. All are configured `--disable-gil`; the 3.14/3.15
  ones are additionally `--with-tail-call-interp`.
- I have not bisected which individual PR in the series is sufficient; the
  branch-level `FT_ATOMIC` count is the evidence that none of them are present.
