"""Command line interface."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from . import __version__
from .gilcheck import installed_top_level_modules, scan as gil_scan
from .model import Severity
from .report import render_markdown, render_text
from .rules import RULES
from .staticscan import scan_tree
from .stress import interpreter_tag, is_freethreaded_build


def _warn_if_gil_build() -> None:
    if not is_freethreaded_build():
        print(
            f"note: running on {interpreter_tag()}; the static scan works anywhere, "
            "but runtime checks need a free-threaded build (python3.14t).",
            file=sys.stderr,
        )


# --------------------------------------------------------------------------- #

def cmd_scan(args: argparse.Namespace) -> int:
    result = scan_tree(
        args.path,
        include_tests=args.include_tests,
        min_severity=Severity(args.min_severity),
    )
    if args.rules:
        wanted = {r.strip().upper() for r in args.rules.split(",")}
        result.findings = [f for f in result.findings if f.rule in wanted]

    if args.json:
        print(result.to_json())
    elif args.markdown:
        print(render_markdown(result, limit=args.limit))
    else:
        print(render_text(result, show_snippets=not args.no_snippets, limit=args.limit))

    if args.generate:
        from .generate import generate

        made = []
        for f in result.sorted_findings():
            g = generate(f, module=args.module or "", module_root=os.path.abspath(args.path),
                         out_dir=args.generate)
            if g:
                made.append(g)
        if made:
            print(f"\ngenerated {len(made)} reproducer(s) in {args.generate}:", file=sys.stderr)
            for g in made:
                print(f"  {g.path}  ({g.kind})", file=sys.stderr)

    counts = result.by_severity()
    if args.fail_on == "none":
        return 0
    threshold = Severity(args.fail_on).rank
    return 1 if any(counts[s] and Severity(s).rank >= threshold for s in counts) else 0


def cmd_gilcheck(args: argparse.Namespace) -> int:
    modules = list(args.modules)
    if args.installed or not modules:
        modules = installed_top_level_modules(args.python)
        if args.exclude:
            skip = {m.strip() for m in args.exclude.split(",")}
            modules = [m for m in modules if m not in skip]
    if not modules:
        print("no modules to check", file=sys.stderr)
        return 2

    result = gil_scan(modules, python=args.python, timeout=args.timeout)
    if args.json:
        print(result.to_json())
        return 1 if result.offenders() else 0

    print(f"ftaudit gilcheck  ({result.interpreter})")
    print(f"  checked {len(result.reports)} module(s)\n")
    offenders = result.offenders()
    crashed = result.crashed()
    if offenders:
        print("GIL RE-ENABLED by importing:")
        for r in offenders:
            print(f"  ! {r.module}")
        print()
    if crashed:
        print("interpreter crashed while importing:")
        for r in crashed:
            first = (r.stderr or "").strip().splitlines()
            print(f"  x {r.module}  (rc={r.returncode}) {first[0][:80] if first else ''}")
        print()
    failed = [r for r in result.reports if not r.ok and not r.crashed]
    if args.verbose and failed:
        print("could not import (not necessarily a problem):")
        for r in failed:
            print(f"  - {r.module}: {r.error}")
        print()
    if not offenders and not crashed:
        print("no module re-enabled the GIL.")
    return 1 if offenders or crashed else 0


def cmd_stress(args: argparse.Namespace) -> int:
    """Run a reproducer repeatedly with the GIL off and on, and compare."""
    script = args.script
    if not os.path.exists(script):
        print(f"no such script: {script}", file=sys.stderr)
        return 2
    exe = args.python or sys.executable

    def campaign(env_extra: dict[str, str], label: str) -> dict:
        env = dict(os.environ)
        env.update(env_extra)
        failures, sigs = 0, {}
        for _ in range(args.trials):
            p = subprocess.run([exe, script, *args.script_args], capture_output=True,
                               text=True, env=env, timeout=args.timeout)
            if p.returncode != 0:
                failures += 1
                key = f"rc={p.returncode}"
                for line in (p.stdout + p.stderr).splitlines():
                    if "Fatal Python error" in line or "Assertion failed" in line:
                        key = line.strip()[:70]
                        break
                sigs[key] = sigs.get(key, 0) + 1
        return {"label": label, "failures": failures, "trials": args.trials, "signatures": sigs}

    off = campaign({"PYTHON_GIL": "0"} if is_freethreaded_build() else {}, "GIL disabled")
    results = [off]
    if not args.no_compare and is_freethreaded_build():
        results.append(campaign({"PYTHON_GIL": "1"}, "GIL enabled"))

    if args.json:
        print(json.dumps({"script": script, "interpreter": interpreter_tag(), "campaigns": results}, indent=2))
    else:
        print(f"ftaudit stress  {script}")
        print(f"  interpreter: {interpreter_tag()}\n")
        for r in results:
            rate = r["failures"] / r["trials"] if r["trials"] else 0
            print(f"  {r['label']:<14} {r['failures']:>3}/{r['trials']} failed ({rate:.0%})")
            for sig, n in sorted(r["signatures"].items(), key=lambda kv: -kv[1])[:3]:
                print(f"      x{n}  {sig}")
        if len(results) == 2 and results[0]["failures"] and not results[1]["failures"]:
            print("\n  => reproduces only with the GIL disabled: a free-threading-specific defect.")
    return 1 if off["failures"] else 0


def cmd_rules(args: argparse.Namespace) -> int:
    for rule in RULES.values():
        print(f"{rule.id}  [{rule.severity.value:<6}] {rule.name}")
        print(f"    {rule.summary}")
        if args.verbose:
            print(f"    why: {rule.why}")
            print(f"    fix: {rule.fix}")
        print()
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    import sysconfig

    print(f"ftaudit {__version__}")
    print(f"  interpreter      : {sys.executable}")
    print(f"  version          : {sys.version.split()[0]}")
    print(f"  free-threaded    : {is_freethreaded_build()}")
    print(f"  GIL enabled now  : {sys._is_gil_enabled() if hasattr(sys, '_is_gil_enabled') else True}")
    print(f"  cpu count        : {os.cpu_count()}")
    ca = sysconfig.get_config_var("CONFIG_ARGS") or ""
    print(f"  tail-call interp : {'--with-tail-call-interp' in ca}")
    return 0


# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ftaudit",
        description="Find thread-safety bugs that the GIL used to hide (PEP 703 / free-threaded Python).",
    )
    p.add_argument("--version", action="version", version=f"ftaudit {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("scan", help="static analysis of a package or file")
    s.add_argument("path")
    s.add_argument("--json", action="store_true")
    s.add_argument("--markdown", action="store_true")
    s.add_argument("--include-tests", action="store_true")
    s.add_argument("--no-snippets", action="store_true")
    s.add_argument("--limit", type=int, default=None)
    s.add_argument("--rules", default=None, help="comma-separated rule ids to keep")
    s.add_argument("--min-severity", default="low", choices=["info", "low", "medium", "high"])
    s.add_argument("--fail-on", default="high", choices=["none", "low", "medium", "high"])
    s.add_argument("--generate", default=None, metavar="DIR", help="write reproducers here")
    s.add_argument("--module", default=None, help="import name used by generated reproducers")
    s.set_defaults(func=cmd_scan)

    g = sub.add_parser("gilcheck", help="find dependencies that re-enable the GIL on import")
    g.add_argument("modules", nargs="*")
    g.add_argument("--installed", action="store_true", help="check every installed distribution")
    g.add_argument("--python", default=None, help="interpreter to probe with")
    g.add_argument("--exclude", default=None)
    g.add_argument("--timeout", type=float, default=120.0)
    g.add_argument("--json", action="store_true")
    g.add_argument("-v", "--verbose", action="store_true")
    g.set_defaults(func=cmd_gilcheck)

    t = sub.add_parser("stress", help="run a reproducer with the GIL off and on, and compare")
    t.add_argument("script")
    t.add_argument("script_args", nargs="*")
    t.add_argument("--trials", type=int, default=20)
    t.add_argument("--timeout", type=float, default=180.0)
    t.add_argument("--python", default=None)
    t.add_argument("--no-compare", action="store_true")
    t.add_argument("--json", action="store_true")
    t.set_defaults(func=cmd_stress)

    r = sub.add_parser("rules", help="list the rule catalogue")
    r.add_argument("-v", "--verbose", action="store_true")
    r.set_defaults(func=cmd_rules)

    i = sub.add_parser("info", help="show interpreter capabilities")
    i.set_defaults(func=cmd_info)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in ("gilcheck", "stress"):
        _warn_if_gil_build()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
