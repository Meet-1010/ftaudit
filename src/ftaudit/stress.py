"""Runtime harness for provoking and measuring data races.

The static pass says "this *looks* racy".  This module is how you prove it.
Everything here is deliberately dependency-free so a generated reproducer can be
mailed to a maintainer as a single file that runs on any 3.13+/3.14 build.

The core trick is :class:`ThreadStress`: every worker blocks on a
:class:`threading.Barrier` and is released simultaneously, so the interesting
window (the gap between a check and the act that follows it) is hit by all
threads at once instead of being serialised by thread-startup cost.
"""

from __future__ import annotations

import os
import statistics
import sys
import sysconfig
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable


def gil_enabled() -> bool:
    """True when this interpreter is currently running with the GIL."""
    fn = getattr(sys, "_is_gil_enabled", None)
    return True if fn is None else bool(fn())


def is_freethreaded_build() -> bool:
    return bool(sysconfig.get_config_var("Py_GIL_DISABLED"))


def default_threads() -> int:
    return max(4, min(32, (os.cpu_count() or 4)))


def interpreter_tag() -> str:
    build = "free-threaded" if is_freethreaded_build() else "gil"
    state = "gil=on" if gil_enabled() else "gil=off"
    return f"{sys.version_info.major}.{sys.version_info.minor} {build} ({state})"


@dataclass
class Failure:
    thread: int
    iteration: int
    exc_type: str
    exc_msg: str
    traceback: str

    def __str__(self) -> str:
        return f"[thread {self.thread} iter {self.iteration}] {self.exc_type}: {self.exc_msg}"


@dataclass
class StressResult:
    """Outcome of one stress campaign."""

    label: str
    threads: int
    iterations: int
    repeats: int
    failures: list[Failure] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    observations: list[Any] = field(default_factory=list)
    trials_run: int = 0
    trials_failed: int = 0
    wall_s: float = 0.0
    interpreter: str = field(default_factory=interpreter_tag)
    gil: bool = field(default_factory=gil_enabled)

    @property
    def ok(self) -> bool:
        return not self.failures and not self.violations

    @property
    def failure_rate(self) -> float:
        return self.trials_failed / self.trials_run if self.trials_run else 0.0

    def summary(self) -> str:
        status = "PASS" if self.ok else "FAIL"
        return (
            f"{status}  {self.label}\n"
            f"  interpreter : {self.interpreter}\n"
            f"  config      : {self.threads} threads x {self.iterations} iterations x {self.repeats} trials\n"
            f"  trials failed: {self.trials_failed}/{self.trials_run} ({self.failure_rate:.0%})\n"
            f"  exceptions  : {len(self.failures)}\n"
            f"  violations  : {len(self.violations)}\n"
            f"  wall        : {self.wall_s:.3f}s"
        )

    def detail(self, limit: int = 5) -> str:
        out = [self.summary()]
        if self.violations:
            out.append("  --- invariant violations ---")
            for v in self.violations[:limit]:
                out.append(f"  * {v}")
            if len(self.violations) > limit:
                out.append(f"  ... and {len(self.violations) - limit} more")
        if self.failures:
            out.append("  --- exceptions ---")
            seen: dict[str, int] = {}
            for f in self.failures:
                key = f"{f.exc_type}: {f.exc_msg}"
                seen[key] = seen.get(key, 0) + 1
            for key, n in sorted(seen.items(), key=lambda kv: -kv[1])[:limit]:
                out.append(f"  * x{n}  {key}")
            out.append("  --- first traceback ---")
            out.append("\n".join("    " + l for l in self.failures[0].traceback.splitlines()))
        return "\n".join(out)

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "ok": self.ok,
            "interpreter": self.interpreter,
            "gil": self.gil,
            "threads": self.threads,
            "iterations": self.iterations,
            "repeats": self.repeats,
            "trials_run": self.trials_run,
            "trials_failed": self.trials_failed,
            "failure_rate": self.failure_rate,
            "wall_s": self.wall_s,
            "violations": self.violations[:50],
            "failures": [
                {"thread": f.thread, "iteration": f.iteration, "type": f.exc_type, "msg": f.exc_msg}
                for f in self.failures[:50]
            ],
            "first_traceback": self.failures[0].traceback if self.failures else "",
        }


class ThreadStress:
    """Run `body` on many threads released from a common barrier."""

    def __init__(
        self,
        threads: int | None = None,
        iterations: int = 200,
        repeats: int = 5,
        label: str = "stress",
        stop_on_first_failure: bool = False,
    ) -> None:
        self.threads = threads or default_threads()
        self.iterations = iterations
        self.repeats = repeats
        self.label = label
        self.stop_on_first_failure = stop_on_first_failure

    def run(
        self,
        body: Callable[[int, int], Any],
        *,
        setup: Callable[[], Any] | None = None,
        check: Callable[[list[Any]], list[str]] | None = None,
        collect: bool = True,
    ) -> StressResult:
        """Execute one campaign.

        ``body(thread_index, iteration)`` is called on every worker.  Anything it
        returns is collected (when ``collect``) and handed to ``check`` after
        each trial; ``check`` returns a list of human-readable invariant
        violations, which is what turns "no exception" into "wrong answer".
        """
        result = StressResult(
            label=self.label, threads=self.threads, iterations=self.iterations, repeats=self.repeats
        )
        t0 = time.perf_counter()
        for trial in range(self.repeats):
            if setup is not None:
                setup()
            barrier = threading.Barrier(self.threads)
            per_thread: list[list[Any]] = [[] for _ in range(self.threads)]
            failures: list[Failure] = []
            fail_lock = threading.Lock()

            def worker(idx: int) -> None:
                try:
                    barrier.wait()
                except threading.BrokenBarrierError:
                    return
                bucket = per_thread[idx]
                for it in range(self.iterations):
                    try:
                        value = body(idx, it)
                        if collect:
                            bucket.append(value)
                    except BaseException as exc:  # noqa: BLE001 - we are the harness
                        with fail_lock:
                            failures.append(
                                Failure(
                                    thread=idx,
                                    iteration=it,
                                    exc_type=type(exc).__name__,
                                    exc_msg=str(exc)[:400],
                                    traceback="".join(
                                        traceback.format_exception(type(exc), exc, exc.__traceback__)
                                    )[:4000],
                                )
                            )
                        break

            workers = [threading.Thread(target=worker, args=(i,), name=f"{self.label}-{i}") for i in range(self.threads)]
            for w in workers:
                w.start()
            for w in workers:
                w.join()

            flat = [v for bucket in per_thread for v in bucket]
            violations = check(flat) if check is not None else []
            result.trials_run += 1
            if failures or violations:
                result.trials_failed += 1
            result.failures.extend(failures)
            result.violations.extend(violations)
            if collect and not result.observations:
                result.observations = flat[:200]
            if self.stop_on_first_failure and (failures or violations):
                break
        result.wall_s = time.perf_counter() - t0
        return result


# --------------------------------------------------------------------------- #
# ready-made probes for the common race shapes
# --------------------------------------------------------------------------- #

def thread_ladder(start: int | None = None, steps: int = 4) -> list[int]:
    """Increasing thread counts, oversubscribing the machine.

    How often a check-then-act race is *observed* scales with how wide its
    window is.  A one-bytecode window (``_X = object()``) is missed at
    one-thread-per-core and shows up reliably at 8x oversubscription, because
    the extra runnable threads force preemption inside the window.  Escalating
    rather than guessing a single thread count is what makes a narrow race
    reproducible instead of "flaky".
    """
    base = start or default_threads()
    return [base * (2 ** i) for i in range(steps)]


def escalate(
    make_result: Callable[[int], StressResult],
    ladder: list[int] | None = None,
) -> StressResult:
    """Run a probe at increasing concurrency, stopping at the first detection.

    Returns the first failing result, or the last clean one if nothing raced.
    """
    last: StressResult | None = None
    for n in ladder or thread_ladder():
        last = make_result(n)
        if not last.ok:
            last.label = f"{last.label} @{n} threads"
            return last
    assert last is not None
    last.label = f"{last.label} @up to {(ladder or thread_ladder())[-1]} threads (clean)"
    return last


def probe_singleton(
    factory: Callable[[], Any],
    reset: Callable[[], None],
    *,
    label: str = "singleton",
    threads: int | None = None,
    repeats: int = 20,
    escalating: bool = False,
) -> StressResult:
    """Assert that a lazily-created singleton is created exactly once.

    ``factory`` is the accessor under test (e.g. ``mod.get_connection``);
    ``reset`` puts the module back into its pre-initialised state so each trial
    starts from a fresh race.
    """
    def check(values: list[Any]) -> list[str]:
        ids = {id(v) for v in values}
        if len(ids) > 1:
            return [
                f"{len(ids)} distinct objects returned where 1 was expected "
                f"({len(values)} calls); the initialiser ran {len(ids)} times"
            ]
        return []

    def once(n: int) -> StressResult:
        return ThreadStress(threads=n, iterations=1, repeats=repeats, label=label).run(
            lambda t, i: factory(), setup=reset, check=check
        )

    if escalating:
        return escalate(once, thread_ladder(threads))
    return once(threads or default_threads())


def probe_lost_update(
    increment: Callable[[], None],
    read: Callable[[], int],
    reset: Callable[[], None],
    *,
    label: str = "lost-update",
    threads: int | None = None,
    iterations: int = 2000,
    repeats: int = 5,
) -> StressResult:
    """Assert that N*M increments really land."""
    stress = ThreadStress(threads=threads, iterations=iterations, repeats=repeats, label=label)
    expected = stress.threads * iterations

    def check(_: list[Any]) -> list[str]:
        got = read()
        if got != expected:
            return [f"expected {expected} increments, observed {got} (lost {expected - got})"]
        return []

    return stress.run(lambda t, i: increment(), setup=reset, check=check, collect=False)


def probe_unique_values(
    produce: Callable[[], Any],
    reset: Callable[[], None] | None = None,
    *,
    label: str = "unique-values",
    threads: int | None = None,
    iterations: int = 500,
    repeats: int = 3,
) -> StressResult:
    """Assert that a shared generator/counter never hands out a duplicate."""
    stress = ThreadStress(threads=threads, iterations=iterations, repeats=repeats, label=label)

    def check(values: list[Any]) -> list[str]:
        seen: set[Any] = set()
        dupes: set[Any] = set()
        for v in values:
            if v in seen:
                dupes.add(v)
            seen.add(v)
        if dupes:
            sample = sorted(dupes, key=repr)[:5]
            return [f"{len(dupes)} duplicate values handed out by a shared iterator, e.g. {sample}"]
        return []

    return stress.run(lambda t, i: produce(), setup=reset, check=check)


def probe_consistency(
    mutate: Callable[[int, int], Any],
    invariant: Callable[[], str | None],
    reset: Callable[[], None] | None = None,
    *,
    label: str = "consistency",
    threads: int | None = None,
    iterations: int = 500,
    repeats: int = 5,
) -> StressResult:
    """Hammer a mutator and re-check a user-supplied invariant after each trial."""
    stress = ThreadStress(threads=threads, iterations=iterations, repeats=repeats, label=label)

    def check(_: list[Any]) -> list[str]:
        msg = invariant()
        return [msg] if msg else []

    return stress.run(mutate, setup=reset, check=check, collect=False)


def probe_global_leak(
    apply_and_restore: Callable[[str], Any],
    observe: Callable[[], Any],
    values: tuple[str, str] = ("A", "B"),
    *,
    label: str = "global-leak",
    iterations: int = 400,
    repeats: int = 5,
) -> StressResult:
    """Two threads temporarily set the same global to different values.

    Any observation of the *other* thread's value proves the save/mutate/restore
    window is visible across threads.
    """
    stress = ThreadStress(threads=2, iterations=iterations, repeats=repeats, label=label)
    leaked: list[str] = []
    lock = threading.Lock()

    def body(idx: int, it: int) -> None:
        mine = values[idx]

        def watcher() -> None:
            seen = observe()
            if seen is not None and seen != mine:
                with lock:
                    leaked.append(f"thread {idx} set {mine!r} but observed {seen!r}")

        apply_and_restore(mine)
        watcher()

    def check(_: list[Any]) -> list[str]:
        if leaked:
            out = leaked[:5]
            leaked.clear()
            return out
        return []

    return stress.run(body, check=check, collect=False)


def timing_stats(samples: list[float]) -> dict[str, float]:
    if not samples:
        return {}
    return {
        "min": min(samples),
        "median": statistics.median(samples),
        "mean": statistics.fmean(samples),
        "max": max(samples),
        "stdev": statistics.pstdev(samples) if len(samples) > 1 else 0.0,
    }
