"""Core data model for ftaudit findings."""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass, field, asdict
from typing import Any


class Severity(str, enum.Enum):
    """How badly a finding can hurt under a free-threaded interpreter."""

    HIGH = "high"      # can corrupt state, crash, or silently produce wrong results
    MEDIUM = "medium"  # racy, usually recoverable or rarely hit
    LOW = "low"        # smell / needs human judgement
    INFO = "info"      # suppressed or contextual

    @property
    def rank(self) -> int:
        return {"high": 3, "medium": 2, "low": 1, "info": 0}[self.value]


class Confidence(str, enum.Enum):
    """How sure the static analyzer is that this is a true positive."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True, slots=True)
class Finding:
    """A single potential thread-safety hazard."""

    rule: str
    name: str
    message: str
    path: str
    line: int
    col: int
    end_line: int
    severity: Severity
    confidence: Confidence
    symbol: str = ""
    function: str = ""
    snippet: str = ""
    why: str = ""
    fix: str = ""
    under_lock: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def location(self) -> str:
        return f"{self.path}:{self.line}"

    @property
    def sort_key(self) -> tuple:
        conf = {"high": 2, "medium": 1, "low": 0}[self.confidence.value]
        return (-self.severity.rank, -conf, self.path, self.line)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["severity"] = self.severity.value
        d["confidence"] = self.confidence.value
        return d


@dataclass
class ScanResult:
    """Everything one static scan produced."""

    target: str
    findings: list[Finding] = field(default_factory=list)
    files_scanned: int = 0
    files_failed: list[tuple[str, str]] = field(default_factory=list)
    native_sources: list[str] = field(default_factory=list)
    duration_s: float = 0.0

    def sorted_findings(self) -> list[Finding]:
        return sorted(self.findings, key=lambda f: f.sort_key)

    def by_severity(self) -> dict[str, int]:
        out = {"high": 0, "medium": 0, "low": 0, "info": 0}
        for f in self.findings:
            out[f.severity.value] += 1
        return out

    def by_rule(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.findings:
            out[f.rule] = out.get(f.rule, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(
            {
                "target": self.target,
                "files_scanned": self.files_scanned,
                "files_failed": self.files_failed,
                "duration_s": round(self.duration_s, 3),
                "summary": {"by_severity": self.by_severity(), "by_rule": self.by_rule()},
                "findings": [f.to_dict() for f in self.sorted_findings()],
            },
            indent=indent,
        )
