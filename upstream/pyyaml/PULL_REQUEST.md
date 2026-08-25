<!-- Draft PR body. Replace NNN with the issue number once the issue is filed. -->
<!-- Branch suggestion: freethreading-registry-locks -->

# TITLE

Make the class-level registries thread-safe

# BODY

Closes #NNN. Part of #870.

## The problem

Every registry classmethod uses the same copy-on-first-write idiom:

```python
if not 'yaml_constructors' in cls.__dict__:
    cls.yaml_constructors = cls.yaml_constructors.copy()
cls.yaml_constructors[tag] = constructor
```

The `cls.__dict__` test and the assignment that publishes the copy are separate
operations. Two threads can both find the registry absent, both copy the inherited
dict, and the second assignment discards the first thread's copy — losing that
registration silently. Measured on a free-threaded 3.14.4 build with 16 threads:
4 of 200 trials lost registrations, up to 10 of 16 in one trial. With
`PYTHON_GIL=1` on the same interpreter: 0 of 200.

## The change

A module-level `threading.RLock` in `constructor.py`, `representer.py` and
`resolver.py` guards each copy-on-write. Registration happens a handful of times
per process, so the lock is not on any hot path.

`add_path_resolver` also gets a small restructure: its copy-on-write sat about
thirty-five lines above the store, with all the argument validation in between.
The validation is pure and stays outside the lock; the copy now happens next to
the store, so the critical section is three lines instead of a whole method.

No public behaviour changes.

## Tests

`tests/test_freethreading.py` adds one test per registry — 16 threads released
from a `threading.Barrier` all register a distinct entry, then the test asserts
every entry survived.

| | Result |
| --- | --- |
| New tests **without** this patch | **4 of 5 fail** |
| New tests **with** this patch | 5 of 5 pass |
| Existing suite, free-threaded 3.14.4t | 1292 passed |
| Existing suite, `PYTHON_GIL=1` | 1292 passed |
| `tests/legacy_tests` | 1280 tests, all pass |
| Reproducer from the issue, after patch | 0 / 400 trials |

The `add_path_resolver` test passes even without the patch — the race there is
latent rather than reproducible, so that one guards against a regression rather
than proving a current failure. I kept it for symmetry; happy to drop it if you
would rather every test in the file be a demonstrated failure.

## Notes on the approach

A few alternatives I considered, in case you prefer one:

- **A lock per class instead of one per module.** Slightly finer-grained, but it
  needs somewhere to live that subclasses do not accidentally share or shadow, and
  registration is rare enough that the contention argument is theoretical.
- **`setdefault` / atomic dict operations.** These do not help, because the hazard
  is the *rebinding* of `cls.yaml_constructors`, not mutation of a single dict.
- **Doing the copy eagerly in `__init_subclass__`.** Removes the check-then-act
  entirely and would be my preference for a larger refactor, but it changes when
  the copy happens and is a bigger behavioural surface than a bug fix should carry.

Environment: CPython 3.14.4 free-threaded (macOS 15 / arm64). Also reproduced on
3.13 free-threaded builds.
