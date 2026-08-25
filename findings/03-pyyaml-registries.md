# PyYAML: concurrent registration silently loses constructors and resolvers

**Status:** filed — [issue #957](https://github.com/yaml/pyyaml/issues/957), [PR #958](https://github.com/yaml/pyyaml/pull/958)
**Severity:** high — silent data loss, no exception
**Upstream home:** [yaml/pyyaml#870 "Free-threaded support blockers"](https://github.com/yaml/pyyaml/issues/870) (open)

## The bug

Every PyYAML registry uses the same copy-on-first-write idiom:

```python
@classmethod
def add_constructor(cls, tag, constructor):
    if not 'yaml_constructors' in cls.__dict__:
        cls.yaml_constructors = cls.yaml_constructors.copy()   # (1) copy
    cls.yaml_constructors[tag] = constructor                    # (2) store
```

Steps (1) and (2) are not atomic, and neither is the `cls.__dict__` test that
guards them. Two threads registering on the same class can both find
`yaml_constructors` absent, both copy the **inherited** dict, and both assign
their copy to `cls.yaml_constructors`. The second assignment discards the
first thread's dict, so that thread's registration is gone — with no error.
The tag simply fails to resolve later, at parse time, far from the cause.

The same shape appears five times:

| File | Method | Registry |
| --- | --- | --- |
| `lib/yaml/constructor.py` | `add_constructor` | `yaml_constructors` |
| `lib/yaml/constructor.py` | `add_multi_constructor` | `yaml_multi_constructors` |
| `lib/yaml/representer.py` | `add_representer` | `yaml_representers` |
| `lib/yaml/representer.py` | `add_multi_representer` | `yaml_multi_representers` |
| `lib/yaml/resolver.py` | `add_implicit_resolver` | `yaml_implicit_resolvers` |
| `lib/yaml/resolver.py` | `add_path_resolver` | `yaml_path_resolvers` |

## Evidence

[`reproducers/pyyaml_add_constructor.py`](reproducers/pyyaml_add_constructor.py),
16 threads registering 16 distinct tags on a fresh `SafeLoader` subclass,
200 trials, PyYAML 6.0.3 on free-threaded 3.14.4:

```console
$ python3.14t reproducers/pyyaml_add_constructor.py
python=3.14.4 gil_enabled=False pyyaml=6.0.3
trials with lost registrations: 4/200
  trial 11: 2/16 registrations lost, e.g. ['!tag13', '!tag15']
  trial 39: 7/16 registrations lost, e.g. ['!tag2', '!tag3', '!tag5', '!tag6']
  trial 109: 10/16 registrations lost, e.g. ['!tag0', '!tag2', '!tag4', '!tag5']

$ PYTHON_GIL=1 python3.14t reproducers/pyyaml_add_constructor.py
trials with lost registrations: 0/200
```

Same interpreter, same wheel — only the GIL differs. Up to **10 of 16**
registrations were lost in a single trial.

## Fix

Guard each copy-on-write with a module-level `threading.RLock`. Registration
happens a handful of times per process, so the lock is free in practice. For
`add_path_resolver` the patch also moves the copy-on-write down next to the
store, so the long validation block no longer sits between them.

## Validation

| Check | Result |
| --- | --- |
| Patch applies to upstream `main` (34a9bf8) | clean |
| PyYAML test suite, free-threaded 3.14.4t | **1292 passed** |
| PyYAML test suite, `PYTHON_GIL=1` | **1292 passed** |
| Legacy suite (`tests/legacy_tests`) | 1280 tests, all pass |
| New regression tests **without** the patch | **4 of 5 fail** |
| New regression tests **with** the patch | 5 of 5 pass |
| Reproducer after patch | 0/400 trials lost a registration |

The patch adds `tests/test_freethreading.py` with one test per registry.

## Fit with upstream

Issue #870 is the open parent for free-threading blockers, and
[#883](https://github.com/yaml/pyyaml/issues/883) proposes multithreaded test
scenarios — but those spawn threads *after* registering constructors, so
concurrent registration itself is not covered. This is a distinct blocker with
a distinct test.

## Caveats

- In most programs registration happens once at import time, single-threaded,
  so the practical exposure is limited to apps that register from worker
  threads or import plugin modules concurrently. It is a real correctness bug
  with a narrow trigger, not an everyday crash.
- `add_path_resolver`'s regression test passes even unpatched; the restructure
  there fixes a latent hazard rather than a demonstrated failure.
