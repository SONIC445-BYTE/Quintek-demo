"""
Runs the frontend data-origin suite under node.

These behaviours are not testable from Python -- they live in the ES modules
the design files import -- but they are load-bearing enough to belong in the
main suite rather than in a scratch script. See
`tests/frontend/data_origin.test.mjs` for what is asserted and why.

Skipped, not failed, when node is unavailable: a Python-only environment
should still be able to run the rest of the suite.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SUITE = ROOT / "tests" / "frontend" / "data_origin.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_frontend_modules_only_use_fixtures_when_no_backend_is_configured():
    result = subprocess.run(["node", str(SUITE)], capture_output=True, text=True,
                            cwd=ROOT, timeout=120)
    # Printed on failure so the failing assertion is visible without rerunning.
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FAIL" not in result.stdout, result.stdout
