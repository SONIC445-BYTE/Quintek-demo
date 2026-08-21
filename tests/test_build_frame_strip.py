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


# ---------------------------------------------------------------------------
# Module discovery
# ---------------------------------------------------------------------------
# A hand-maintained module list went stale and the APK shipped without the
# student engine or billing: both imports rejected, both `.catch()` handlers
# set the API to null, and the app came up looking merely "not configured".

def test_modules_are_discovered_from_the_file(tmp_path) -> None:
    from tools_build_standalone import discover_modules

    for name in ("a.js", "b.js"):
        (tmp_path / name).write_text("export const x = 1;")
    html = """
      import('./a.js').then(f);
      import("./b.js").catch(g);
      import( './a.js' );
    """
    assert discover_modules(html, tmp_path) == ["a.js", "b.js"]


def test_a_file_with_no_imports_needs_no_modules(tmp_path) -> None:
    from tools_build_standalone import discover_modules

    assert discover_modules("<div>nothing here</div>", tmp_path) == []


def test_importing_a_module_that_does_not_exist_fails_the_build(tmp_path) -> None:
    from tools_build_standalone import MissingModule, discover_modules

    with pytest.raises(MissingModule) as exc:
        discover_modules("import('./absent.js')", tmp_path)
    assert "absent.js" in str(exc.value)
    assert "unconfigured" in str(exc.value)


def test_every_module_the_app_imports_is_inlined_in_the_bundle() -> None:
    """
    The property that was actually violated. Checked against the built file,
    not against the source list, because the source list is what was wrong.
    """
    from pathlib import Path

    from tools_build_standalone import IMPORT_RE, discover_modules

    source = Path("frontend/PG Revision.dc.html").read_text(encoding="utf-8")
    expected = discover_modules(source, Path("frontend"))
    assert expected, "the app imports no modules at all -- has the wiring gone?"

    built = Path("frontend/dist/pg-revision.html")
    if not built.exists():
        pytest.skip("no build present; run tools_build_standalone.py")
    html = built.read_text(encoding="utf-8")
    for name in expected:
        assert f"__DC_MOD['{name}']" in html, f"{name} was not inlined into the bundle"


def test_no_dynamic_import_survives_into_a_bundle() -> None:
    """One left behind is a feature that works in a browser and is absent from
    the APK -- and reports itself as unconfigured rather than broken."""
    from pathlib import Path

    from tools_build_standalone import IMPORT_RE

    for built in Path("frontend/dist").glob("*.html"):
        leftover = set(IMPORT_RE.findall(built.read_text(encoding="utf-8")))
        assert not leftover, f"{built.name} still imports {sorted(leftover)} at runtime"


def test_the_billing_surface_reaches_the_android_asset() -> None:
    """
    The APK loads from android/app/src/main/assets. A bundle that is correct in
    frontend/dist and stale in assets is the exact shape of "I rebuilt it and
    the emulator did not change".
    """
    from pathlib import Path

    asset = Path("android/app/src/main/assets/pg-revision.html")
    dist = Path("frontend/dist/pg-revision.html")
    if not (asset.exists() and dist.exists()):
        pytest.skip("no build present")
    assert asset.read_bytes() == dist.read_bytes(), (
        "the Android asset differs from the build output; rebuild before running"
        " the emulator")
    assert "__DC_MOD['quintek-billing-api.js']" in asset.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The Android WebView must inject every backend global the bundle reads
# ---------------------------------------------------------------------------
# Real bug: the injector set only `window.__QUINTEK_API__`. The learner engine
# and billing clients read `window.__QUINTEK_STUDENT_API__` instead, so on
# Android the student screen's notebooks, generation and billing were
# permanently "not configured" -- with a correct backend URL saved in
# Settings -- and only the AI-transparency screen ever connected.

def test_every_backend_global_the_bundle_reads_is_injected_by_android() -> None:
    from pathlib import Path

    kotlin = Path(
        "android/app/src/main/java/com/quintek/app/WebScreenActivity.kt"
    ).read_text(encoding="utf-8")

    # Only the backend-ORIGIN globals belong here. The token globals
    # (__QUINTEK_STUDENT_TOKEN__, __QUINTEK_ADMIN_TOKEN__) are set by the
    # app's own login flow at runtime, after a user signs in inside the
    # WebView -- there is nothing for the native host to inject before the
    # page has even loaded.
    for js_file in Path("frontend").glob("*.js"):
        text = js_file.read_text(encoding="utf-8")
        for m in re.finditer(r"window\.(__QUINTEK_\w*API__)", text):
            name = m.group(1)
            assert f"window.{name} = " in kotlin, (
                f"{js_file.name} reads window.{name}, but "
                "WebScreenActivity.kt never injects it -- on Android that "
                "module will report itself unconfigured even when a backend "
                "URL is saved in Settings")
