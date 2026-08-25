"""For a module that re-enables the GIL, find which extension actually did it."""
import json, subprocess, sys

PY = ".venv-ft/bin/python"
PROBE = r"""
import sys, importlib, json, warnings
warnings.simplefilter("ignore")
name = sys.argv[1]
importlib.import_module(name)
ext = []
for mod, m in list(sys.modules.items()):
    f = getattr(m, "__file__", None) or ""
    if f.endswith((".so", ".pyd", ".dylib")):
        ext.append((mod, f))
print("JSON:" + json.dumps({"gil": sys._is_gil_enabled(), "ext": ext}))
"""
ISOLATE = r"""
import sys, importlib, json, warnings
warnings.simplefilter("ignore")
name = sys.argv[1]
try:
    importlib.import_module(name)
    ok, err = True, None
except BaseException as e:
    ok, err = False, f"{type(e).__name__}: {e}"[:120]
print("JSON:" + json.dumps({"mod": name, "gil": sys._is_gil_enabled(), "ok": ok, "err": err}))
"""

def run(code, arg):
    p = subprocess.run([PY, "-c", code, arg], capture_output=True, text=True, timeout=180)
    for line in p.stdout.splitlines():
        if line.startswith("JSON:"):
            return json.loads(line[5:])
    return None

for target in sys.argv[1:]:
    print(f"\n=== {target} ===")
    info = run(PROBE, target)
    if not info:
        print("  probe failed"); continue
    print(f"  gil after import: {info['gil']}")
    exts = [m for m, f in info["ext"]]
    print(f"  extension modules loaded: {len(exts)}")
    culprits = []
    for mod, f in info["ext"]:
        r = run(ISOLATE, mod)
        if r and r["ok"] and r["gil"]:
            culprits.append((mod, f))
    if culprits:
        print("  --> GIL re-enabled by importing, in isolation:")
        for mod, f in culprits:
            print(f"        {mod}")
            print(f"          {f}")
    else:
        print("  --> no single extension re-enabled it in isolation")
        for mod, f in info["ext"][:15]:
            print(f"        (loaded) {mod}")
