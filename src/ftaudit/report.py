"""Human-readable rendering of scan results."""

from __future__ import annotations

import os
import shutil
import sys

from .model import Finding, ScanResult, Severity
from .rules import RULES

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text

_SEV_STYLE = {
    "high": lambda s: _c("1;31", s),
    "medium": lambda s: _c("1;33", s),
    "low": lambda s: _c("36", s),
    "info": lambda s: _c("2", s),
}


def render_text(result: ScanResult, *, show_snippets: bool = True, limit: int | None = None) -> str:
    width = min(shutil.get_terminal_size((100, 24)).columns, 110)
    out: list[str] = []
    findings = result.sorted_findings()
    if limit:
        findings = findings[:limit]

    out.append(_c("1", f"ftaudit  {result.target}"))
    counts = result.by_severity()
    out.append(
        f"  {result.files_scanned} files scanned in {result.duration_s:.2f}s  |  "
        + "  ".join(
            _SEV_STYLE[k](f"{counts[k]} {k}") for k in ("high", "medium", "low") if counts[k]
        )
        or "  no findings"
    )
    if result.files_failed:
        out.append(_c("2", f"  {len(result.files_failed)} files could not be parsed"))
    out.append("")

    current_file = None
    for f in findings:
        if f.path != current_file:
            current_file = f.path
            out.append(_c("1;4", f.path))
        sev_label = _SEV_STYLE[f.severity.value](f"{f.severity.value.upper():<6}")
        head = f"  {sev_label} {_c('1', f.rule)} line {f.line}  {_c('2', 'in ' + f.function)}"
        out.append(head)
        out.append(f"    {f.message}")
        if show_snippets and f.snippet:
            for line in f.snippet.splitlines()[:5]:
                out.append(_c("2", f"      | {line[:width-8]}"))
        out.append(_c("2", f"    fix: {f.fix.splitlines()[0][:width-10]}"))
        out.append("")

    if result.findings:
        out.append(_c("1", "by rule:"))
        for rule_id, n in result.by_rule().items():
            rule = RULES.get(rule_id)
            name = rule.name if rule else rule_id
            out.append(f"  {rule_id}  {n:>4}  {name}")
    return "\n".join(out)


def render_markdown(result: ScanResult, limit: int | None = None) -> str:
    findings = result.sorted_findings()
    if limit:
        findings = findings[:limit]
    counts = result.by_severity()
    out = [
        f"# ftaudit report — `{result.target}`",
        "",
        f"- files scanned: **{result.files_scanned}**",
        f"- high: **{counts['high']}**, medium: **{counts['medium']}**, low: **{counts['low']}**",
        f"- duration: {result.duration_s:.2f}s",
        "",
        "## Findings",
        "",
        "| Severity | Rule | Location | Symbol | Message |",
        "| --- | --- | --- | --- | --- |",
    ]
    for f in findings:
        msg = f.message.replace("|", "\\|")
        out.append(
            f"| {f.severity.value} | {f.rule} | `{f.path}:{f.line}` | `{f.symbol}` | {msg} |"
        )
    return "\n".join(out)
