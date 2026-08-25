"""Rule catalogue: identity, default severity, and the human explanation.

Each rule documents *why* the pattern is a hazard specifically under a
free-threaded (PEP 703) interpreter, and what the accepted fix looks like.
Keeping the prose here means the CLI, the JSON report and the generated
reproducer files all quote the same explanation.
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import Severity


@dataclass(frozen=True, slots=True)
class Rule:
    id: str
    name: str
    severity: Severity
    summary: str
    why: str
    fix: str


def _r(*args) -> Rule:
    return Rule(*args)


RULES: dict[str, Rule] = {
    r.id: r
    for r in [
        _r(
            "FT101",
            "global-rebind",
            Severity.MEDIUM,
            "Function rebinds a module-level global",
            "With the GIL removed there is no implicit serialisation between the read "
            "and the write of a module global. Two threads calling this function can "
            "interleave so that one thread's write is lost, or a third thread observes "
            "a half-initialised value.",
            "Move the state into an explicit object owned by the caller, or guard every "
            "read and write with a module-level threading.Lock.",
        ),
        _r(
            "FT102",
            "lazy-init-race",
            Severity.HIGH,
            "Check-then-act lazy initialisation of shared state",
            "`if X is None: X = expensive()` is two bytecode-separated operations. Under "
            "free-threading N threads can all observe None and all run the initialiser. "
            "That duplicates side effects (open files, spawned threads, registered "
            "handles) and, worse, lets one thread publish an object that another thread "
            "is still mutating.",
            "Guard with a module-level lock and re-check inside it, or precompute the "
            "value at import time, or use functools.cache (whose C implementation is "
            "atomic) when the initialiser is genuinely pure.",
        ),
        _r(
            "FT103",
            "check-then-act-container",
            Severity.HIGH,
            "Membership test on a shared container followed by mutation",
            "`if k not in cache: cache[k] = compute(k)` is not atomic. Two threads can "
            "both miss, both compute, and both store. If the stored object carries "
            "identity (a connection, a lock, a registry entry) callers end up holding "
            "different objects that were supposed to be the same one.",
            "Use dict.setdefault() / collections.defaultdict, which are atomic at the "
            "C level, or hold a lock across the test and the mutation.",
        ),
        _r(
            "FT104",
            "shared-container-mutation",
            Severity.MEDIUM,
            "Unguarded mutation of a module-level mutable container",
            "The container object itself is internally locked in CPython, so the "
            "interpreter will not crash, but the *sequence* of operations a caller "
            "performs is not atomic. Concurrent mutation from many threads makes the "
            "container's contents depend on scheduling.",
            "Take a lock around the whole logical operation, or make the state per-thread "
            "(threading.local) or per-instance.",
        ),
        _r(
            "FT105",
            "read-modify-write",
            Severity.HIGH,
            "Read-modify-write on shared state without a lock",
            "`counter += 1` compiles to LOAD / BINARY_OP / STORE. With the GIL these "
            "rarely interleaved; without it they interleave constantly and increments "
            "are silently lost. This is the single most reproducible free-threading bug.",
            "Use itertools.count().__next__, a lock, or move the counter into per-thread "
            "state and aggregate at the end.",
        ),
        _r(
            "FT106",
            "process-global-mutation",
            Severity.HIGH,
            "Library code mutates interpreter- or process-wide state",
            "os.environ, locale, warnings filters, signal handlers, sys.path, the "
            "recursion limit and numpy's error state are shared by every thread in the "
            "process. A library that flips them affects unrelated threads. No amount of "
            "locking inside the library can fix this, because the observers are outside it.",
            "Prefer an explicit parameter or a context object. If the global really must "
            "change, do it once at import/configure time, or document the call as "
            "not-thread-safe and expose a non-global alternative.",
        ),
        _r(
            "FT107",
            "save-mutate-restore",
            Severity.HIGH,
            "Global state is temporarily changed and restored",
            "The save/mutate/restore idiom is only correct if nothing else runs "
            "meanwhile. Under free-threading another thread executes inside the window "
            "and sees the temporary value; two threads doing it concurrently restore "
            "each other's saved value and leave the global permanently wrong.",
            "Thread the value through as a parameter, or move it to a threading.local / "
            "contextvars.ContextVar so each thread has its own copy.",
        ),
        _r(
            "FT108",
            "broken-double-checked-lock",
            Severity.HIGH,
            "Double-checked locking without a re-check inside the lock",
            "Checking the sentinel outside the lock and then initialising inside it "
            "without re-checking defeats the lock entirely: two threads can pass the "
            "outer check, queue on the lock, and initialise one after the other.",
            "Re-test the sentinel after acquiring the lock (the classic "
            "`if X is None:\\n  with lock:\\n    if X is None: X = ...` shape).",
        ),
        _r(
            "FT109",
            "shared-iterator",
            Severity.MEDIUM,
            "A module-level iterator/generator is advanced from a function",
            "Generators are not re-entrant. Advancing the same generator from two "
            "threads raises ValueError('generator already executing') or, for hand-written "
            "iterators, silently yields duplicate or skipped values.",
            "Create the iterator per call, or wrap next() in a lock.",
        ),
        _r(
            "FT110",
            "class-attribute-mutation",
            Severity.MEDIUM,
            "Instance method mutates class-level (shared) state",
            "A class attribute is shared by every instance in every thread. Mutating it "
            "from a method turns per-instance work into cross-thread interference.",
            "Assign the attribute on the instance in __init__ instead of on the class.",
        ),
        _r(
            "FT111",
            "useless-fresh-lock",
            Severity.HIGH,
            "A lock is constructed at the point of use, so it protects nothing",
            "`with threading.Lock():` allocates a brand-new lock that no other thread "
            "can contend on. It reads as synchronisation but provides none. Under the "
            "GIL the surrounding code often happened to be atomic anyway; under "
            "free-threading the real race is exposed.",
            "Hoist the lock to module or instance scope so all threads share one object.",
        ),
        _r(
            "FT112",
            "native-ext-no-ft-declaration",
            Severity.HIGH,
            "C/Cython extension does not declare free-threading support",
            "An extension module that does not set Py_mod_gil = Py_MOD_GIL_NOT_USED "
            "(or, for Cython, `# cython: freethreading_compatible=True`) makes CPython "
            "re-enable the GIL for the whole process at import time. One such dependency "
            "silently removes free-threading from every other library in the program.",
            "Audit the extension for thread safety, then add the Py_mod_gil slot via "
            "multi-phase init (or the Cython directive) and ship a cp3XXt wheel.",
        ),
        _r(
            "FT113",
            "finalizer-shared-mutation",
            Severity.MEDIUM,
            "__del__ / finalizer mutates shared state",
            "Finalizers already ran at unpredictable times; under free-threading they "
            "also run on an unpredictable *thread*, concurrently with normal code that "
            "touches the same state.",
            "Keep finalizers to releasing resources the object exclusively owns, or take "
            "the same lock the rest of the code uses.",
        ),
        _r(
            "FT114",
            "unsynchronised-cached-property",
            Severity.MEDIUM,
            "cached_property with a side-effecting body",
            "functools.cached_property lost its internal lock in Python 3.12, so the "
            "getter can run more than once concurrently. That is harmless for a pure "
            "computation but not when the body registers, opens, spawns or counts.",
            "Make the body pure, or compute the value eagerly in __init__, or guard it "
            "with an explicit lock.",
        ),
    ]
}


def get(rule_id: str) -> Rule:
    return RULES[rule_id]


#: Attribute/function targets that mutate interpreter- or process-wide state.
#: Keyed by the dotted call target; value is a short description used in the message.
PROCESS_GLOBALS: dict[str, str] = {
    "os.putenv": "the process environment",
    "os.unsetenv": "the process environment",
    "os.chdir": "the process working directory",
    "os.umask": "the process umask",
    "os.setuid": "the process credentials",
    "os.setgid": "the process credentials",
    "sys.setrecursionlimit": "the interpreter recursion limit",
    "sys.settrace": "the interpreter trace hook",
    "sys.setprofile": "the interpreter profile hook",
    "sys.setswitchinterval": "the interpreter switch interval",
    "locale.setlocale": "the process locale",
    "signal.signal": "process signal handlers",
    "signal.alarm": "the process alarm timer",
    "signal.setitimer": "the process interval timer",
    "warnings.simplefilter": "the global warnings filter",
    "warnings.filterwarnings": "the global warnings filter",
    "warnings.resetwarnings": "the global warnings filter",
    "warnings.catch_warnings": "the global warnings filter (catch_warnings is documented as not thread-safe)",
    "random.seed": "the shared module-level random.Random instance",
    "random.setstate": "the shared module-level random.Random instance",
    "decimal.setcontext": "the decimal context",
    "time.tzset": "the process timezone",
    "faulthandler.enable": "the process fault handler",
    "gc.disable": "the interpreter garbage collector",
    "gc.enable": "the interpreter garbage collector",
    "gc.set_threshold": "the interpreter garbage collector",
    "numpy.seterr": "numpy's global floating-point error state",
    "np.seterr": "numpy's global floating-point error state",
    "numpy.set_printoptions": "numpy's global print options",
    "np.set_printoptions": "numpy's global print options",
    "numpy.random.seed": "numpy's shared legacy global RandomState",
    "np.random.seed": "numpy's shared legacy global RandomState",
    "matplotlib.use": "the matplotlib backend (process-wide)",
    "torch.set_default_dtype": "PyTorch's global default dtype",
    "torch.set_grad_enabled": "PyTorch's global autograd flag",
    "torch.manual_seed": "PyTorch's global RNG",
    "pandas.set_option": "pandas' global option registry",
    "pd.set_option": "pandas' global option registry",
}

#: Subscript assignment targets that are process-global, e.g. os.environ["X"] = ...
PROCESS_GLOBAL_SUBSCRIPTS: dict[str, str] = {
    "os.environ": "the process environment",
    "sys.modules": "the interpreter module table",
    "sys.path": "the interpreter import path",
}

#: Names that, when constructed, produce a lock-like object.
LOCK_CONSTRUCTORS: frozenset[str] = frozenset(
    {
        "threading.Lock",
        "threading.RLock",
        "threading.Condition",
        "threading.Semaphore",
        "threading.BoundedSemaphore",
        "threading.Barrier",
        "Lock",
        "RLock",
        "Condition",
        "Semaphore",
        "BoundedSemaphore",
        "Barrier",
        "_thread.allocate_lock",
        "allocate_lock",
        "multiprocessing.Lock",
        "multiprocessing.RLock",
        "asyncio.Lock",
    }
)

#: Callables that build a mutable container.
CONTAINER_CONSTRUCTORS: dict[str, str] = {
    "dict": "mapping",
    "list": "sequence",
    "set": "set",
    "bytearray": "sequence",
    "collections.defaultdict": "mapping",
    "defaultdict": "mapping",
    "collections.OrderedDict": "mapping",
    "OrderedDict": "mapping",
    "collections.Counter": "mapping",
    "Counter": "mapping",
    "collections.deque": "sequence",
    "deque": "sequence",
    "collections.ChainMap": "mapping",
    "weakref.WeakValueDictionary": "mapping",
    "WeakValueDictionary": "mapping",
    "weakref.WeakKeyDictionary": "mapping",
    "WeakKeyDictionary": "mapping",
    "weakref.WeakSet": "set",
    "WeakSet": "set",
}

#: Methods that mutate their receiver in place.
MUTATING_METHODS: frozenset[str] = frozenset(
    {
        "append", "extend", "insert", "remove", "pop", "clear", "sort", "reverse",
        "add", "discard", "update", "setdefault", "popitem", "appendleft",
        "extendleft", "popleft", "rotate", "difference_update", "intersection_update",
        "symmetric_difference_update", "__setitem__", "__delitem__",
    }
)

#: Mutating methods that are nevertheless atomic in CPython and therefore safe
#: to call concurrently *as a single operation*.
ATOMIC_METHODS: frozenset[str] = frozenset({"setdefault", "append", "add", "popleft", "appendleft"})
