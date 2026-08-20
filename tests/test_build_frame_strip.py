"""
The shipped app must not contain a picture of a phone.

`strip_device_frame` makes every substitution optional, because a given
design file may legitimately lack a given element. The cost is silence: a
restyled design stops matching, the strip does nothing, and the app ships
with the mockup's carpet and a painted-on status bar next to the real one.

That shipped, and was diagnosed from a screenshot rather than from the build.
These tests are the build noticing instead.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from tools_build_standalone import (FrameNotStripped, _FRAME_MARKERS, strip_device_frame,
                                    verify_frame_removed)

ROOT = Path(__file__).resolve().parent.parent
DESIGN = ROOT / "frontend" / "PG Revision.dc.html"


def test_the_guard_rejects_every_frame_marker():
    for marker, _why in _FRAME_MARKERS:
        with pytest.raises(FrameNotStripped):
            verify_frame_removed(f"<div>{marker}</div>", "test")


def test_the_guard_names_what_it_found_and_what_to_do():
    with pytest.raises(FrameNotStripped) as excinfo:
        verify_frame_removed("border-radius:42px", "PG Revision")
    message = str(excinfo.value)
    assert "rounded corners" in message
    assert "do not ship the frame" in message


def test_clean_output_passes():
    verify_frame_removed("<div>a real application</div>", "test")


def test_the_real_design_file_strips_cleanly():
    """If this fails, a design change outran a pattern in strip_device_frame."""
    stripped = strip_device_frame(DESIGN.read_text(encoding="utf-8"), "PG Revision")
    verify_frame_removed(stripped, "PG Revision")


def test_the_painted_status_bar_is_removed():
    """
    Two clocks disagreeing is worse than none: the real device draws its own.
    """
    source = DESIGN.read_text(encoding="utf-8")
    assert re.search(r"<span>\d{2}:\d{2}</span>", source), (
        "the design file no longer contains a painted clock; this test's premise is stale")
    stripped = strip_device_frame(source, "PG Revision")
    assert not re.search(r"<span>\d{2}:\d{2}</span>", stripped)


def test_the_fixed_viewport_becomes_full_screen():
    stripped = strip_device_frame(DESIGN.read_text(encoding="utf-8"), "PG Revision")
    assert "width:390px;height:844px" not in stripped
    assert "min-height:100vh" in stripped


def test_the_builder_runs_and_produces_frameless_output(tmp_path):
    """End to end: the actual script, on the actual design files."""
    result = subprocess.run([sys.executable, "tools_build_standalone.py", "PG Revision"],
                            capture_output=True, text=True, cwd=ROOT, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr

    built = (ROOT / "frontend" / "dist" / "pg-revision.html").read_text(encoding="utf-8")
    for marker, why in _FRAME_MARKERS:
        assert marker not in built, f"{marker!r} ({why}) survived into the shipped build"
