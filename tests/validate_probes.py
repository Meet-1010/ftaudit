"""Prove the probes detect the races in tests/corpus/racy.py."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "corpus"))

import racy
from ftaudit.stress import interpreter_tag, probe_singleton, probe_lost_update, probe_unique_values

print(f"interpreter: {interpreter_tag()}\n")

def reset_instance():
    racy._INSTANCE = None

r1 = probe_singleton(racy.get_instance, reset_instance, label="FT102 get_instance()", repeats=40)
print(r1.summary()); print("  ->", r1.violations[:2], "\n")

def reset_counter():
    racy._COUNTER = 0

r2 = probe_lost_update(racy.bump, lambda: racy._COUNTER, reset_counter,
                       label="FT105 bump()", iterations=5000, repeats=3)
print(r2.summary()); print("  ->", r2.violations[:2], "\n")

import itertools
def reset_ticker():
    racy._TICKER = itertools.count()

r3 = probe_unique_values(racy.tick, reset_ticker, label="FT109 tick()", iterations=2000, repeats=3)
print(r3.summary()); print("  ->", r3.violations[:1], "\n")

# FT103: cache identity - two threads must get the *same* cached object
def reset_cache():
    racy._CACHE.clear()
from ftaudit.stress import ThreadStress
def check_identity(values):
    ids = {}
    for v in values:
        ids.setdefault(id(v), 0)
    return [f"{len(ids)} distinct objects for one cache key"] if len(ids) > 1 else []
r4 = ThreadStress(iterations=1, repeats=60, label="FT103 memo()").run(
    lambda t, i: racy.memo((1,)), setup=reset_cache, check=check_identity)
print(r4.summary()); print("  ->", r4.violations[:1], "\n")

failed = sum(1 for r in (r1, r2, r3, r4) if not r.ok)
print(f"RESULT: {failed}/4 probes detected a race on this interpreter")
sys.exit(0)
