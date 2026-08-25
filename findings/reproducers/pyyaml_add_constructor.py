#!/usr/bin/env python3
"""PyYAML: concurrent add_constructor() silently loses registrations.

yaml/constructor.py::BaseConstructor.add_constructor is

    @classmethod
    def add_constructor(cls, tag, constructor):
        if not 'yaml_constructors' in cls.__dict__:
            cls.yaml_constructors = cls.yaml_constructors.copy()
        cls.yaml_constructors[tag] = constructor

The copy-on-first-write is a check-then-act.  Two threads that both find
'yaml_constructors' missing from cls.__dict__ each copy the *inherited* dict and
each assign the copy to cls.yaml_constructors.  The second assignment discards
the first thread's copy, so whichever registration landed in the discarded dict
is silently lost -- no exception, just a tag that will later fail to resolve.

Exit 0 = every registration survived; 1 = registrations were lost.
"""
import sys
import threading

import yaml

THREADS = int(sys.argv[1]) if len(sys.argv) > 1 else 16
TRIALS = int(sys.argv[2]) if len(sys.argv) > 2 else 200


def make_loader():
    # A fresh subclass per trial, so yaml_constructors starts out inherited.
    return type("TrialLoader", (yaml.SafeLoader,), {})


def main() -> int:
    lost_trials = []
    for trial in range(TRIALS):
        loader = make_loader()
        barrier = threading.Barrier(THREADS)

        def worker(i: int) -> None:
            barrier.wait()
            loader.add_constructor(f"!tag{i}", lambda l, n: i)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        registered = loader.yaml_constructors
        missing = [f"!tag{i}" for i in range(THREADS) if f"!tag{i}" not in registered]
        if missing:
            lost_trials.append((trial, len(missing), missing[:4]))

    print(f"python={sys.version.split()[0]} gil_enabled={sys._is_gil_enabled()} "
          f"pyyaml={yaml.__version__}")
    print(f"trials with lost registrations: {len(lost_trials)}/{TRIALS}")
    for trial, n, sample in lost_trials[:5]:
        print(f"  trial {trial}: {n}/{THREADS} registrations lost, e.g. {sample}")
    return 1 if lost_trials else 0


if __name__ == "__main__":
    raise SystemExit(main())
