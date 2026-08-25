# certifi: concurrent `where()` orphans resource context managers

**Status:** filed — [certifi/python-certifi#433](https://github.com/certifi/python-certifi/pull/433)
**Severity:** medium — resource leak; worst case a CA bundle path deleted while in use
**File:** `certifi/core.py`

## The bug

```python
global _CACERT_CTX
global _CACERT_PATH
if _CACERT_PATH is None:
    _CACERT_CTX = as_file(files("certifi").joinpath("cacert.pem"))
    _CACERT_PATH = str(_CACERT_CTX.__enter__())
    atexit.register(exit_cacert_ctx)
return _CACERT_PATH
```

Classic check-then-act. Concurrent callers all pass the `is None` test; each
opens its own context manager and registers its own `atexit` hook, but only the
last assignment to `_CACERT_CTX` survives. The earlier context managers are
orphaned — nothing holds them, and `exit_cacert_ctx` runs once per extra
registration against whichever context happens to be current.

When certifi is imported from a zip or wheel, `as_file` **extracts a temporary
file** and `__exit__` deletes it. Then an orphaned context is a temp file
nobody cleans up, and the duplicate `atexit` hooks can delete a path another
thread is still holding.

## Evidence

[`reproducers/certifi_where_init.py`](reproducers/certifi_where_init.py),
16 threads, 200 trials, counting entries into the resource context:

```console
$ python3.14t reproducers/certifi_where_init.py 16 200
python=3.14.4 gil_enabled=False
trials where the resource context was entered != 1 time: 200/200
  trial 0: entered 16 times -> 15 orphaned context manager(s)
  trial 3: entered 2 times  -> 1 orphaned context manager(s)

$ PYTHON_GIL=1 python3.14t reproducers/certifi_where_init.py 16 200
trials where the resource context was entered != 1 time: 1/200
  trial 0: entered 16 times -> 15 orphaned context manager(s)
```

Note the GIL-enabled run: it fails on trial 0 too. This is **not** a
free-threading-only bug — it is a pre-existing race that the GIL hid after the
first call warmed the cache. Free-threading turns an occasional bug into a
reliable one (200/200 vs 1/200).

## Fix

Double-checked locking, with `_CACERT_PATH` published **last** so no thread can
see a path whose context manager is not yet reachable for cleanup. Both the
`sys.version_info >= (3, 11)` branch and the fallback branch are patched.

## Validation

| Check | Result |
| --- | --- |
| certifi test suite | 3 passed |
| Reproducer after patch | 0/200 trials |
| Reproducer before patch | 200/200 trials |

## Caveats

- In the common non-zip install, `as_file` returns the real filesystem path and
  `__exit__` is a no-op, so the practical damage is bounded to redundant
  `atexit` registrations. The severe case (temp-file extraction) needs certifi
  to be imported from a zipimport/wheel context.
- Because this reproduces with the GIL on as well, it should be filed as a
  general thread-safety bug rather than a free-threading regression.
