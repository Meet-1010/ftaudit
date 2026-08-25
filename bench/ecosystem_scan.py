"""Static-scan every installed distribution and rank by hazard density."""
import json, os, sys
sys.path.insert(0, "src")
from ftaudit.staticscan import scan_tree
from ftaudit.model import Severity

SITE = sys.argv[1]
SKIP = {"ftaudit", "pip", "__pycache__"}
rows = []
for entry in sorted(os.listdir(SITE)):
    path = os.path.join(SITE, entry)
    if entry.endswith((".dist-info", ".pth", ".py", ".so")) or entry in SKIP:
        continue
    if not os.path.isdir(path):
        continue
    try:
        r = scan_tree(path, include_tests=False, min_severity=Severity.LOW)
    except Exception as exc:
        print(f"  {entry}: scan error {exc}", file=sys.stderr); continue
    if r.files_scanned == 0:
        continue
    c = r.by_severity()
    rows.append({
        "package": entry, "files": r.files_scanned,
        "high": c["high"], "medium": c["medium"], "low": c["low"],
        "total": c["high"] + c["medium"] + c["low"],
        "by_rule": r.by_rule(),
        "duration_s": round(r.duration_s, 2),
        "findings": [f.to_dict() for f in r.sorted_findings() if f.severity == Severity.HIGH][:400],
    })

rows.sort(key=lambda x: (-x["high"], -x["total"]))
json.dump(rows, open("findings/ecosystem_scan.json", "w"), indent=1)

tot_files = sum(r["files"] for r in rows)
tot_high = sum(r["high"] for r in rows)
print(f"scanned {len(rows)} packages / {tot_files} files -> {tot_high} high-severity findings\n")
print(f"{'package':<26}{'files':>6}{'high':>6}{'med':>6}{'low':>6}   top rules")
for r in rows[:30]:
    top = ", ".join(f"{k}:{v}" for k, v in list(r["by_rule"].items())[:3])
    print(f"{r['package']:<26}{r['files']:>6}{r['high']:>6}{r['medium']:>6}{r['low']:>6}   {top}")
