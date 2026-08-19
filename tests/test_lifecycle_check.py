"""
Runs `tools_lifecycle_check.py` as part of the suite.

The script is useful to run by hand, but a whole-pipeline check that only runs
when someone remembers to run it will rot. This keeps it honest.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_all_thirteen_phases_pass_over_live_http():
    result = subprocess.run([sys.executable, str(ROOT / "tools_lifecycle_check.py")],
                            capture_output=True, text=True, cwd=ROOT, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "13/13 phases verified" in result.stdout, result.stdout
