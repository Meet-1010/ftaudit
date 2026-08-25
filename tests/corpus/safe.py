"""Patterns that are already correct. None of these should be reported."""
import functools
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

_LOCK = threading.Lock()
_RLOCK = threading.RLock()
_CACHE = {}
_INSTANCE = None
_LOCAL = threading.local()
CONSTANT_MAP = {"a": 1}          # only read
_BUILT_AT_IMPORT = {}
for _i in range(3):
    _BUILT_AT_IMPORT[_i] = _i     # module level: runs once, under the import lock


def correct_dcl():
    global _INSTANCE
    if _INSTANCE is None:
        with _LOCK:
            if _INSTANCE is None:
                _INSTANCE = object()
    return _INSTANCE


def locked_write(key, value):
    with _LOCK:
        _CACHE[key] = value


def locked_rmw(key):
    with _RLOCK:
        _CACHE[key] = _CACHE.get(key, 0) + 1


def atomic_setdefault(key):
    return _CACHE.setdefault(key, [])


@functools.cache
def pure(n):
    return n * n


def per_thread(value):
    _LOCAL.value = value
    return _LOCAL.value


def only_reads(key):
    if key in CONSTANT_MAP:
        return CONSTANT_MAP[key]
    return None


def local_state():
    seen = {}
    items = []
    count = 0
    for i in range(10):
        if i not in seen:
            seen[i] = i
        items.append(i)
        count += 1
    return seen, items, count


class Session:
    default_timeout = 30          # immutable class attr

    def __init__(self):
        self._items = []          # per instance
        self._lock = threading.Lock()
        self._n = 0

    def add(self, item):
        self._items.append(item)

    def bump(self):
        with self._lock:
            self._n += 1


@functools.cached_property
def _unused(self):
    return 1


def scoped_numpy_state(x):
    # numpy made errstate context-local in 2.0, so a seterr inside it is
    # restored per-thread and is not a cross-thread hazard.
    import numpy as np
    with np.errstate():
        np.seterr(divide="ignore", invalid="ignore")
        return x


def scoped_decimal_context(x):
    import decimal
    with decimal.localcontext() as ctx:
        ctx.prec = 50
        return +x
