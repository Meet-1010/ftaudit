# Findings

Everything here was reproduced on macOS 15 / arm64 (Apple M5, 10 cores) against
free-threaded CPython builds installed with `uv python install`.

Every runtime claim is stated as **_n_ failures / _m_ trials**, and every one
was re-run with `PYTHON_GIL=1` on the same interpreter and the same wheels, so
"this is a free-threading bug" is a measured A/B rather than an assertion.

| # | Target | Finding | Deliverable |
| --- | --- | --- | --- |
| [01](01-cpython-generator-crash.md) | CPython 3.13t / 3.14t | Concurrent generator iteration segfaults the interpreter. Fixed on `main`, never backported. | [issue #156351](https://github.com/python/cpython/issues/156351) |
| [06](06-sympy-dummy-index.md) | SymPy 1.14.0 | Concurrent `Dummy()` collides on `dummy_index`, so distinct dummies compare equal | [PR #30340](https://github.com/sympy/sympy/pull/30340) |
| [02](02-gil-reenabled-packages.md) | lxml, grpcio, SQLAlchemy | Undeclared extensions re-enable the GIL for the whole process | measurement + per-project reports |
| [03](03-pyyaml-registries.md) | PyYAML 6.0.3 | Concurrent `add_constructor` / `add_*_resolver` silently loses registrations | [#957](https://github.com/yaml/pyyaml/issues/957) + [PR #958](https://github.com/yaml/pyyaml/pull/958) |
| [04](04-werkzeug-logger.md) | Werkzeug | Duplicate log handler installed on concurrent first log | [PR #3258](https://github.com/pallets/werkzeug/pull/3258) |
| [05](05-certifi-where.md) | certifi | `where()` orphans resource context managers | [PR #433](https://github.com/certifi/python-certifi/pull/433) |

## Layout

- `reproducers/` — standalone, dependency-free scripts. Exit 0 = no race
  observed, 1 = invariant violated. Each one runs on any 3.13+/3.14 build.
- `patches/` — unified diffs against each project's `main`, generated from a
  clone so they apply directly.
- `crash_matrix.json`, `crash_matrix.txt` — raw cross-interpreter data for #01.
- `gilcheck.json` — raw output of `ftaudit gilcheck --installed` for #02.
- `ecosystem_scan.json`, `ecosystem_scan.txt` — raw static findings across 85 packages.
- `../upstream/` — ready-to-file issue and pull-request text per project.

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
triggers, not everyday crashes, and the write-ups say so.

The two with broad reach are 01 and 06. Sharing a generator across a thread pool
is ordinary and the failure is an uncatchable process death; and SymPy's `Dummy`
collision produces a *wrong answer* with no error at all, which is the worst
failure mode in the set.

One finding was investigated and **dropped**: `np.seterr()` inside
`numpy/ma/core.py` looked like process-global mutation on a hot path, but numpy
made `errstate` context-local in 2.0 and a two-thread test confirmed no leakage.
That was a false positive in FT106, and the rule was fixed rather than the
non-bug reported.
