"""Run every test_* function in tests/ without pytest: python tests/run_all.py"""
import importlib.util, sys, traceback, pathlib, os, tempfile
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="vdcfg")
os.environ["XDG_DATA_HOME"] = tempfile.mkdtemp(prefix="vddata")
ok = fail = 0
for f in sorted(pathlib.Path(__file__).resolve().parent.glob("test_*.py")):
    spec = importlib.util.spec_from_file_location(f.stem, f); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    for n in dir(m):
        if n.startswith("test_") and callable(getattr(m, n)):
            try:
                getattr(m, n)(); ok += 1
            except Exception:
                fail += 1; print("FAIL", f.name, n); traceback.print_exc()
print(f"{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
