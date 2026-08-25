# Werkzeug: concurrent first log call installs the handler twice

**Status:** filed — [pallets/werkzeug#3258](https://github.com/pallets/werkzeug/pull/3258)
**Severity:** medium — duplicated log output, user-visible
**File:** `src/werkzeug/_internal.py`

## The bug

```python
global _logger

if _logger is None:
    _logger = logging.getLogger("werkzeug")           # published immediately

    if _logger.level == logging.NOTSET:
        _logger.setLevel(logging.INFO)

    if not _has_level_handler(_logger):               # check-then-act
        _logger.addHandler(_ColorStreamHandler())
```

Two check-then-act races in one function:

1. `_logger is None` — several threads can pass it together.
2. `_has_level_handler(_logger)` — each of them then sees "no handler yet" and
   adds its own.

There is also a publication bug: `_logger` is assigned *before* the level and
handler are configured, so another thread can pick up a logger that is not yet
set up and log through it.

The visible result is every werkzeug log line printed once per extra handler.

## Evidence

[`reproducers/werkzeug_logger_init.py`](reproducers/werkzeug_logger_init.py),
16 threads emitting their first log line simultaneously, 300 trials:

```console
$ python3.14t reproducers/werkzeug_logger_init.py 16 300
python=3.14.4 gil_enabled=False
trials with the wrong handler count: 4/300
  trial 6: 2 handlers installed (expected 1) -> every werkzeug log line printed 2 times

$ PYTHON_GIL=1 python3.14t reproducers/werkzeug_logger_init.py 16 300
trials with the wrong handler count: 0/300
```

## Fix

Double-checked locking with a module-level `threading.Lock`, and — importantly
— assign `_logger` **last**, after the level and handler are configured, so no
thread can observe a half-built logger.

## Validation

| Check | Result |
| --- | --- |
| Werkzeug test suite (excl. `test_serving`/`test_debug`, which fail to collect in this env for unrelated reasons) | **995 passed** |
| Reproducer after patch | 0/300 trials |
| Reproducer before patch | 4/300 trials |

## Caveats

- Requires the *first* werkzeug log line to be emitted concurrently, which in
  a normal dev-server startup happens once from one thread. The exposure is
  real but narrow: apps that first log from a thread pool, or test suites
  running werkzeug apps in parallel threads.
- `test_serving.py` and `test_debug.py` could not be collected in this
  environment (missing `fsevents` backend, unregistered pytest mark) — both
  unrelated to this change, but it means they are unverified.
