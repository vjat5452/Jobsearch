"""Run every test script: python tests/run_all.py"""
import subprocess
import sys
from pathlib import Path

here = Path(__file__).parent
failed = []
for t in sorted(here.glob("test_*.py")):
    r = subprocess.run([sys.executable, str(t)], cwd=str(here))
    if r.returncode:
        failed.append(t.name)
print("\nALL PASS" if not failed else f"\nFAILED: {', '.join(failed)}")
sys.exit(1 if failed else 0)
