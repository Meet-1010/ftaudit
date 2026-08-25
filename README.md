# ftaudit — find the thread-safety bugs the GIL used to hide

PEP 779 made free-threaded Python officially supported in 3.14. The porting
problem that follows is not "does my package import" — it is "which of my
check-then-act patterns were only ever correct because two bytecodes rarely
interleaved".

The existing tooling runs a project's **existing tests** in parallel. That
catches a lot, but the official free-threading guide is explicit about the gap:
it "cannot discover issues from multithreaded use of data structures defined by
your library". Existing tests mostly exercise one object per test; the races
live in the shared, module-level state those tests never contend on.

`ftaudit` targets that gap with three passes:

| Command | Question it answers |
| --- | --- |
| `ftaudit scan` | Which code *looks* racy under PEP 703? (AST, lock-aware, 14 rules) |
| `ftaudit gilcheck` | Which installed dependency silently turns the GIL **back on**? |
| `ftaudit stress` | Does this reproducer actually race — and only without the GIL? |

Zero third-party dependencies, so it runs on a bare free-threaded interpreter
where much of the ecosystem still has no wheels.

## Install

```bash
uv python install 3.14t
uv venv --python 3.14t .venv-ft
uv pip install --python .venv-ft/bin/python -e .
```

## Use

Static scan of a package:

```bash
ftaudit scan path/to/package
```

```
ftaudit  tests/corpus/racy.py
  1 files scanned in 0.00s  |  13 high  8 medium

racy.py
  HIGH   FT105 line 21  in bump
    `_COUNTER +=` is a non-atomic read-modify-write on shared state
  HIGH   FT102 line 32  in get_instance
    `_INSTANCE` is lazily initialised with an unsynchronised check-then-act
    fix: Guard with a module-level lock and re-check inside it, or precompute
```

Find dependencies that kill free-threading for the whole process:

```bash
ftaudit gilcheck --installed
```

```
ftaudit gilcheck  (3.14.4 free-threaded, gil=off)
  checked 81 module(s)

GIL RE-ENABLED by importing:
  ! grpc
  ! soupsieve
  ! sqlalchemy
```

`soupsieve` is pure Python — it shows up because it imports `lxml`, whose
`lxml.etree` extension is the actual culprit. That transitivity is the whole
point: one undeclared extension anywhere in the dependency tree removes
free-threading from the entire process.

Prove a race is free-threading-specific — same interpreter, same wheels, GIL
toggled with `PYTHON_GIL`:

```bash
ftaudit stress findings/reproducers/pyyaml_add_constructor.py --trials 20
```

```
  GIL disabled    20/20 failed (100%)
  GIL enabled      0/20 failed (0%)

  => reproduces only with the GIL disabled: a free-threading-specific defect.
```

## The rules

| ID | Name | What it catches |
| --- | --- | --- |
| FT101 | `global-rebind` | a function rebinds a module global |
| FT102 | `lazy-init-race` | `if X is None: X = ...` on shared state |
| FT103 | `check-then-act-container` | membership test then mutate a shared container |
| FT104 | `shared-container-mutation` | unguarded mutation of module-level containers |
| FT105 | `read-modify-write` | `counter += 1` on shared state |
| FT106 | `process-global-mutation` | `os.environ`, locale, warnings filters, signals, `np.seterr` |
| FT107 | `save-mutate-restore` | global temporarily changed and restored |
| FT108 | `broken-double-checked-lock` | outer check, no re-check inside the lock |
| FT109 | `shared-iterator` | a module-level generator advanced from a function |
| FT110 | `class-attribute-mutation` | a method mutates class-level state |
| FT111 | `useless-fresh-lock` | `with threading.Lock():` — protects nothing |
| FT112 | `native-ext-no-ft-declaration` | C/Cython source with no `Py_mod_gil` declaration |
| FT113 | `finalizer-shared-mutation` | `__del__` mutating shared state |
| FT114 | `unsynchronised-cached-property` | `cached_property` with a side-effecting body |

`ftaudit rules -v` prints the full rationale and suggested fix for each.

### Keeping the noise down

A linter for races is only useful if you believe its output, so the analyser:

- **only walks function bodies.** Module-level mutation runs once at import,
  under the import lock; it is not a race.
- **tracks lock depth.** A hazard inside `with self._lock:` is recorded as
  `INFO`, not reported as a defect.
- **understands correct double-checked locking** and stays quiet about it,
  while flagging the version that forgets the inner re-check (FT108).
- **treats `dict.setdefault` and `list.append` as atomic**, because they are.
- **subsumes** weaker findings into stronger ones, so one racy cache lookup is
  reported once instead of three times.
- **knows which "process globals" are actually context-local.** A `np.seterr`
  inside `with np.errstate():` is restored per-thread since numpy 2.0, so FT106
  stays quiet there — `warnings.catch_warnings` deliberately does not get that
  pass, because it is still documented as thread-unsafe.
- honours `# noqa: FT102` and `# ftaudit: ignore`.

`tests/corpus/safe.py` holds the *correct* version of every pattern in
`racy.py`, and the test suite asserts it produces zero actionable findings.
That pairing is the contract that keeps the rules honest.

## Proving a race, not just suspecting one

Static findings are hypotheses. `ftaudit.stress` turns them into evidence:

- workers are released from a `threading.Barrier`, so every thread hits the
  check-then-act window at once instead of being serialised by thread startup;
- probes assert an **invariant** (`probe_singleton`, `probe_lost_update`,
  `probe_unique_values`, `probe_global_leak`), so "no exception" is not
  mistaken for "no bug";
- `escalating=True` ramps the thread count. Detection probability scales with
  how wide the race window is — a one-bytecode window is missed at
  one-thread-per-core and shows up at 8× oversubscription;
- everything can be re-run under `PYTHON_GIL=1` for a controlled A/B on the
  **same interpreter and the same wheels**.

## What it found

Run against 85 packages / 4 963 files of the installed scientific-Python and
web stack. Full write-ups in [`findings/`](findings/); ready-to-file issue and
PR text in [`upstream/`](upstream/).

| # | Finding | Status |
| --- | --- | --- |
| [01](findings/01-cpython-generator-crash.md) | **CPython segfaults** when two threads iterate one generator, on free-threaded 3.13/3.14. Fixed in 3.15, never backported. 150 crashes / 180 runs. | [issue #156351](https://github.com/python/cpython/issues/156351) |
| [06](findings/06-sympy-dummy-index.md) | **SymPy** hands out colliding `dummy_index` values, so symbols meant to be distinct compare **equal** — silently wrong maths. 73% collided. | [PR #30340](https://github.com/sympy/sympy/pull/30340) |
| [02](findings/02-gil-reenabled-packages.md) | **lxml, grpcio, SQLAlchemy** silently re-enable the GIL process-wide. Importing `bs4` is enough. | reports ready, not filed |
| [03](findings/03-pyyaml-registries.md) | **PyYAML** loses constructor/resolver registrations under concurrent registration. | [issue #957](https://github.com/yaml/pyyaml/issues/957) · [PR #958](https://github.com/yaml/pyyaml/pull/958) |
| [04](findings/04-werkzeug-logger.md) | **Werkzeug** installs its log handler twice; every line printed twice. | patch ready, not filed |
| [05](findings/05-certifi-where.md) | **certifi** orphans resource context managers in `where()`. | patch ready, not filed |

## Development

```bash
.venv-ft/bin/python -m pytest tests/ -q     # 32 tests
```

CI runs the suite on 3.13t / 3.14t / 3.15t / 3.14 across Linux, macOS and
Windows, re-runs every reproducer on each platform (the findings were first
measured on macOS/arm64 only), and scans ftaudit's own source — which is
expected to come back clean.

## Licence

MIT.
