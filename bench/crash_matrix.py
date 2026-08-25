"""Cross-version reproduction matrix for the shared-generator crash."""
import glob, os, re, subprocess, sys, json

SCRIPT = sys.argv[1] if len(sys.argv) > 1 else "findings/reproducers/gen_concurrent_next.py"
TRIALS = int(os.environ.get("TRIALS", "15"))
UVR = os.path.expanduser("~/.local/share/uv/python")

def find(pattern, exe):
    hits = sorted(glob.glob(os.path.join(UVR, pattern, "bin", exe)))
    return hits[0] if hits else None

INTERPRETERS = [
    ("3.13.13t", find("cpython-3.13.13+freethreaded-*", "python3.13t"), {}),
    ("3.14.0t",  find("cpython-3.14.0+freethreaded-*",  "python3.14t"), {}),
    ("3.14.4t",  find("cpython-3.14*+freethreaded-*",   "python3.14t"), {}),
    ("3.15.0a8t",find("cpython-3.15*+freethreaded-*",   "python3.15t"), {}),
    ("3.14.4t GIL=1", find("cpython-3.14*+freethreaded-*", "python3.14t"), {"PYTHON_GIL": "1"}),
    ("3.14.7 (GIL build)", "/opt/homebrew/bin/python3.14", {}),
]

SIG = re.compile(r"(Fatal Python error: [A-Za-z_]+|Assertion failed: \([^)]*\)|Segmentation fault)")

def run(name, binpath, env_extra, args):
    if not binpath or not os.path.exists(binpath):
        return {"name": name, "available": False}
    env = dict(os.environ); env.update(env_extra)
    crashes, sigs, rcs = 0, {}, {}
    for _ in range(TRIALS):
        p = subprocess.run([binpath, SCRIPT, *args], capture_output=True, text=True, env=env, timeout=120)
        if p.returncode != 0:
            crashes += 1
            rcs[p.returncode] = rcs.get(p.returncode, 0) + 1
            m = SIG.search(p.stdout + p.stderr)
            key = m.group(1) if m else f"signal/rc={p.returncode}"
            sigs[key] = sigs.get(key, 0) + 1
    ver = subprocess.run([binpath, "-c", "import sys,sysconfig;print(sys.version.split()[0], sys._is_gil_enabled(), bool(sysconfig.get_config_var('Py_GIL_DISABLED')))"],
                         capture_output=True, text=True, env=env).stdout.strip()
    return {"name": name, "available": True, "version_gil_ft": ver,
            "crashes": crashes, "trials": TRIALS, "signatures": sigs, "returncodes": rcs}

CONFIGS = [("2 threads x 3000, trivial gen", ["2","3000","trivial"]),
           ("8 threads x 20000, counter gen", ["8","20000","counter"]),
           ("16 threads x 50000, yield-from range", ["16","50000","range"])]

out = {}
for label, args in CONFIGS:
    print(f"\n=== {label} ===")
    rows = []
    for name, binpath, env_extra in INTERPRETERS:
        r = run(name, binpath, env_extra, args)
        rows.append(r)
        if not r["available"]:
            print(f"  {name:<22} (not installed)"); continue
        sig = ", ".join(f"{k} x{v}" for k, v in sorted(r["signatures"].items(), key=lambda kv: -kv[1])[:2])
        print(f"  {name:<22} {r['crashes']:>2}/{r['trials']} crashed   {sig}")
    out[label] = rows
json.dump(out, open("findings/crash_matrix.json","w"), indent=2)
print("\nwrote findings/crash_matrix.json")
