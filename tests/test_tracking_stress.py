"""An isolated process contains any native solver crash during stress checks."""
import json
from pathlib import Path
import subprocess
import sys


def test_concurrent_real_solver_movement_groups_and_stale_readings():
    helper = Path(__file__).parent / "helpers" / "tracking_stress.py"
    completed = subprocess.run([sys.executable, str(helper)], capture_output=True,
                               text=True, timeout=65, check=False)
    assert completed.returncode == 0, (completed.returncode, completed.stdout, completed.stderr)
    result = json.loads(completed.stdout)
    assert result["workers"] == 12
    assert result["iterations"] == 2048
    assert result["beacon_fixes"] > 3900
