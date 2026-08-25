"""Deliberately thread-unsafe patterns. Every rule should fire exactly once here."""
import functools
import itertools
import os
import sys
import threading
import warnings

_REGISTRY = {}
_CACHE = {}
_ITEMS = []
_COUNTER = 0
_INSTANCE = None
_CONN = None
_LOCK = threading.Lock()
_TICKER = itertools.count()


def bump():                       # FT101 + FT105
    global _COUNTER
    _COUNTER += 1
    return _COUNTER


def rebind(x):                    # FT101
    global _INSTANCE
    _INSTANCE = x


def get_instance():               # FT102
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = object()
    return _INSTANCE


def memo(key):                    # FT103
    if key not in _CACHE:
        _CACHE[key] = key * 2
    return _CACHE[key]


def memo_get(key):                # FT103 (get-miss form)
    val = _CACHE.get(key)
    if val is None:
        val = key * 3
        _CACHE[key] = val
    return val


def register(name):               # FT104
    _ITEMS.append(name)
    _REGISTRY.update({name: 1})


def widen_path(p):                # FT106
    sys.path.insert(0, p)
    os.environ["FT_FLAG"] = "1"
    sys.setrecursionlimit(5000)


def quiet():                      # FT106 (catch_warnings)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return 1


def with_temp_env(value):         # FT107
    old = os.environ.get("MODE")
    try:
        os.environ["MODE"] = value
        return do_work()
    finally:
        os.environ["MODE"] = old


def do_work():
    return 0


def broken_dcl():                 # FT108
    global _CONN
    if _CONN is None:
        with _LOCK:
            _CONN = object()
    return _CONN


def tick():                       # FT109
    return next(_TICKER)


class Registry:
    _shared = []                  # class-level mutable
    _count = 0

    def add(self, item):          # FT110 (self._shared is class-level, never in __init__)
        self._shared.append(item)

    def bump(self):               # FT110
        type(self)._count += 1

    def __del__(self):            # FT113
        _ITEMS.append("gone")

    @functools.cached_property
    def handle(self):             # FT114
        self._opened = True
        return open(os.devnull)


def useless_lock():               # FT111
    with threading.Lock():
        _ITEMS.append(1)
