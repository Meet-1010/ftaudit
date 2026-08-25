# Findings

Everything here was reproduced on macOS 15 / arm64 (Apple M5, 10 cores) against
free-threaded CPython builds installed with `uv python install`.

Every runtime claim is stated as **_n_ failures / _m_ trials**, and every one
was re-run with `PYTHON_GIL=1` on the same interpreter and the same wheels, so
"this is a free-threading bug" is a measured A/B rather than an assertion.

| # | Target | Finding | Deliverable |
| --- | --- | --- | --- |
| [01](01-cpython-generator-crash.md) | CPython 3.13t / 3.14t | Concurrent generator iteration segfaults the interpreter. Fixed on `main`, never backported. | reproducer + version matrix + backport request |
| [02](02-gil-reenabled-packages.md) | lxml, grpcio, SQLAlchemy | Undeclared extensions re-enable the GIL for the whole process | measurement + per-project reports |
| [03](03-pyyaml-registries.md) | PyYAML 6.0.3 | Concurrent `add_constructor` / `add_*_resolver` silently loses registrations | patch + 5 regression tests |
| [04](04-werkzeug-logger.md) | Werkzeug | Duplicate log handler installed on concurrent first log | patch |
| [05](05-certifi-where.md) | certifi | `where()` orphans resource context managers | patch |

## Layout

- `reproducers/` — standalone, dependency-free scripts. Exit 0 = no race
  observed, 1 = invariant violated. Each one runs on any 3.13+/3.14 build.
- `patches/` — unified diffs against each project's `main`, generated from a
  clone so they apply directly.
- `crash_matrix.json`, `crash_matrix.txt` — raw cross-interpreter data for #01.
- `gilcheck.json` — raw output of `ftaudit gilcheck --installed` for #02.
- `ecosystem_scan.json` — raw static findings across 79 packages.

## Reproducing

```bash
uv python install 3.14t 3.15         # 3.15 is the control that should pass
uv venv --python 3.14t .venv-ft
uv pip install --python .venv-ft/bin/python -e . -r bench/ecosystem.txt

.venv-ft/bin/ftaudit gilcheck --installed
python3.14 bench/crash_matrix.py findings/reproducers/gen_concurrent_next.py
python3.14 bench/ecosystem_scan.py .venv-ft/lib/python3.14t/site-packages
```

## A note on severity

Two of these (03, 04) need registration or first-log to happen concurrently,
which many programs never do. They are real correctness bugs with narrow
triggers, not everyday crashes, and the write-ups say so. Finding 01 is the
one with broad reach: sharing a generator across a thread pool is ordinary,
and the failure is an uncatchable process death.
