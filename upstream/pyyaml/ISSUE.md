<!-- Draft for https://github.com/yaml/pyyaml/issues/new  -->
<!-- Suggested labels: none (maintainers triage); link under #870 -->

# TITLE

Registry classmethods lose registrations when called concurrently (free-threading blocker)

# BODY

Sub-issue of #870.

## Summary

All six registry classmethods use the same copy-on-first-write idiom, and it is a
check-then-act. Under the free-threaded build two threads registering on the same
class can each copy the *inherited* registry and the second assignment discards
the first thread's copy — that registration is lost with no error. The tag or type
simply fails to resolve later, far from the cause.

```python
@classmethod
def add_constructor(cls, tag, constructor):
    if not 'yaml_constructors' in cls.__dict__:
        cls.yaml_constructors = cls.yaml_constructors.copy()   # (1) copy
    cls.yaml_constructors[tag] = constructor                    # (2) store
```

Neither the `cls.__dict__` test nor the gap between (1) and (2) is atomic.

Affected:

| File | Method | Registry |
| --- | --- | --- |
| `lib/yaml/constructor.py` | `add_constructor` | `yaml_constructors` |
| `lib/yaml/constructor.py` | `add_multi_constructor` | `yaml_multi_constructors` |
| `lib/yaml/representer.py` | `add_representer` | `yaml_representers` |
| `lib/yaml/representer.py` | `add_multi_representer` | `yaml_multi_representers` |
| `lib/yaml/resolver.py` | `add_implicit_resolver` | `yaml_implicit_resolvers` |
| `lib/yaml/resolver.py` | `add_path_resolver` | `yaml_path_resolvers` |

`add_implicit_resolver` is the worst case: it rebuilds the whole dict-of-lists
before publishing it, so a lost update discards an entire set of resolvers rather
than one entry.

## Reproducer

16 threads registering 16 distinct tags on a fresh `SafeLoader` subclass,
200 trials, PyYAML 6.0.3:

```python
import sys, threading, yaml

THREADS, TRIALS = 16, 200

def main():
    lost = []
    for trial in range(TRIALS):
        loader = type("TrialLoader", (yaml.SafeLoader,), {})
        barrier = threading.Barrier(THREADS)

        def worker(i):
            barrier.wait()
            loader.add_constructor(f"!tag{i}", lambda l, n: i)

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(THREADS)]
        for t in ts: t.start()
        for t in ts: t.join()

        missing = [f"!tag{i}" for i in range(THREADS)
                   if f"!tag{i}" not in loader.yaml_constructors]
        if missing:
            lost.append((trial, len(missing), missing[:4]))

    print(f"gil_enabled={sys._is_gil_enabled()} pyyaml={yaml.__version__}")
    print(f"trials with lost registrations: {len(lost)}/{TRIALS}")
    for trial, n, sample in lost[:5]:
        print(f"  trial {trial}: {n}/{THREADS} lost, e.g. {sample}")
    return 1 if lost else 0

raise SystemExit(main())
```

## Result

```console
$ python3.14t repro.py
gil_enabled=False pyyaml=6.0.3
trials with lost registrations: 4/200
  trial 11: 2/16 lost, e.g. ['!tag13', '!tag15']
  trial 39: 7/16 lost, e.g. ['!tag2', '!tag3', '!tag5', '!tag6']
  trial 109: 10/16 lost, e.g. ['!tag0', '!tag2', '!tag4', '!tag5']

$ PYTHON_GIL=1 python3.14t repro.py
gil_enabled=True pyyaml=6.0.3
trials with lost registrations: 0/200
```

Same interpreter, same wheel — the GIL is the only variable. Up to **10 of 16**
registrations were lost in a single trial.

## Why the tests in #883 do not catch this

The scenarios proposed there spawn threads *after* having registered a constructor
and a resolver, so they exercise concurrent *use* of the registries — which is the
right thing to test — but not concurrent *registration*, which is where this bug
lives.

## Suggested fix

Guard each copy-on-write with a module-level `threading.RLock`. Registration
happens a handful of times per process, so the cost is not measurable, and an
`RLock` keeps the door open for a subclass whose registration path re-enters.

For `add_path_resolver` the copy-on-write currently sits ~35 lines above the store,
with argument validation in between; moving it down next to the store makes the
critical section small and obvious.

I have a patch and five regression tests (one per registry) and will open a PR
referencing this issue — the tests fail on 4 of 5 registries without the fix and
pass with it, and the existing suite stays green on both a free-threaded and a
GIL-enabled interpreter. Happy to rework the approach if you would rather solve
it a different way.

## Environment

- CPython 3.14.4 free-threaded (`--disable-gil`, `--with-tail-call-interp`), macOS 15 / arm64, 10 cores
- PyYAML 6.0.3, and reproduced against `main` at 34a9bf8
- Also present on 3.13 free-threaded builds

## Caveat

In most programs registration happens once at import time on a single thread, so
the practical exposure is limited to applications that register from worker threads
or import plugin modules concurrently. It is a real correctness bug with a narrow
trigger rather than an everyday failure — but it is silent when it does happen,
which is what makes it worth closing before free-threaded support is announced.
