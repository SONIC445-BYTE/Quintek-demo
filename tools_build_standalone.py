"""
Build a self-contained, offline-capable HTML file from a design-component
(`.dc.html`) source.

The `.dc.html` files are authored against a small runtime that expects three
things the file itself does not contain:

  1. React + ReactDOM, fetched at boot from unpkg.com by `support.js`.
  2. Sibling scripts `./support.js` and `./image-slot.js`.
  3. ES modules pulled in at runtime via `import('./quintek-eval-api.js')`
     and `import('./quintek-report-api.js')`.

That is fine when the files are served together from a directory, and it is
why opening one directly fails wherever unpkg is unreachable -- including the
Artifact host, whose CSP admits no external origin. This script resolves all
three so the result runs from a single file with **zero network requests**.

How each is handled:

  React      Inlined ahead of `support.js` from `frontend/vendor/`, pinned to
             the 18.3.1 UMD builds the runtime asks for. `loadReactUmd()`
             short-circuits on `window.React && window.ReactDOM`, so no
             patching of `support.js` is needed -- it simply never reaches the
             CDN branch.

  Scripts    `<script src="./x.js">` is replaced by the file's contents.

  Modules    `import('./quintek-eval-api.js')` cannot work without a real file
             beside the document, and rewriting it to a blob: or data: URL
             puts module loading at the mercy of the host CSP. Instead each
             module is evaluated ONCE at boot inside an async IIFE that
             resolves to the same shape the module exported, and the dynamic
             import is rewritten to that promise. The consuming code does
             `import('...').then(api => ...)`, so a promise is a faithful
             substitute -- and the live-backend seam inside the modules keeps
             working, because the fetch still happens in that IIFE.

Usage:

    python3 tools_build_standalone.py                    # build all
    python3 tools_build_standalone.py "PG Revision"      # build one

Output lands in `frontend/dist/`.
"""

from __future__ import annotations

import base64
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / "frontend"
VENDOR = FRONTEND / "vendor"
DIST = FRONTEND / "dist"
ANDROID_ASSETS = ROOT / "android" / "app" / "src" / "main" / "assets"

# Only these two ship inside the APK. The harness and audit screens are
# read-only views over hardcoded fixtures -- checked before excluding them:
# the harness's three click handlers are all navigation, the audit has none,
# and neither performs a fetch. Leaving them out costs no functionality and
# halves the app's size.
ANDROID_SCREENS = {"pg-revision.html", "quintek-admin.html"}

# The .dc.html sources to build, and the title each standalone should carry.
TARGETS = {
    "PG Revision": ("PG Revision.dc.html", "Quintek PG Revision"),
    "Quintek Admin": ("Quintek Admin.dc.html", "Quintek Admin Console"),
    "Quintek Harness": ("Quintek Harness.dc.html", "Quintek Harness"),
    "Quintek Audit": ("Quintek Audit.dc.html", "Quintek Audit"),
}

# ES modules the .dc.html files import at runtime, in dependency order.
# Which ES modules a design file pulls in is DISCOVERED, not listed.
#
# It used to be a hand-maintained list, and it went stale: the student engine
# and the whole billing surface were added to the app, imported at runtime, and
# never inlined. In the browser those imports resolve against sibling files and
# everything works. In the single-file build and therefore in the APK they
# cannot resolve at all -- and each import is wrapped in a `.catch()` that sets
# the API to null, so the app came up looking merely "not configured" while
# actually shipping without payments or generation.
#
# A list that has to be updated by hand every time a module is added is a list
# that will be out of date the first time somebody forgets.
IMPORT_RE = re.compile(r"""import\(\s*['"]\./([A-Za-z0-9._-]+\.js)['"]\s*\)""")


class MissingModule(SystemExit):
    """A design file imports a module that is not on disk, so the build would
    ship an app whose import silently rejects."""


def discover_modules(html: str, root: Path) -> list[str]:
    """Every `import('./x.js')` in the file, in a stable order."""
    found = []
    for name in IMPORT_RE.findall(html):
        if name not in found:
            found.append(name)
    missing = [n for n in found if not (root / n).exists()]
    if missing:
        raise MissingModule(
            "design file imports modules that do not exist: "
            + ", ".join(missing)
            + ". The build would have produced an app whose import rejects"
              " silently and reports itself unconfigured.")
    return found


class FrameError(SystemExit):
    """A device-frame rule matched nothing, so the build would have shipped a
    phone mockup as the app UI without saying so."""


def _sub_once(pattern: str, repl: str, html: str, what: str, *, required=True,
              flags=re.S) -> str:
    """Apply one frame rule and refuse to continue if it silently missed.

    Every rule below targets specific markup in the design files. If a design
    is re-exported and that markup shifts, a regex that quietly matches nothing
    would leave the mockup chrome in a shipped app -- exactly the defect this
    pass exists to remove. Failing the build is the only way that stays
    visible.
    """
    out, n = re.subn(pattern, repl, html, flags=flags)
    if n == 0 and required:
        raise FrameError(f"device-frame rule matched nothing: {what}")
    return out


def strip_device_frame(html: str, name: str) -> str:
    """
    Turn a design-component preview into an application shell.

    The `.dc.html` files are design artefacts: each screen is drawn sitting on
    a dark "carpet" inside a bordered, rounded, fixed-size device mockup, under
    a caption naming the viewport, with a painted-on status bar showing 07:12.
    That is right for reviewing a design and wrong for running one -- on a real
    phone it renders a picture of a phone, inside a phone, with two status bars
    and a letterboxed app that cannot use the screen it was given.

    The design files keep their frames. This pass runs at build time.
    """
    # 1. The carpet: full-bleed background with the screen centred in padding.
    html = _sub_once(
        r"min-height:100vh;box-sizing:border-box;background:(#[0-9a-fA-F]{3,8});"
        r"display:flex;flex-direction:column;align-items:center;gap:\d+px;padding:[^;\"]*",
        r"min-height:100vh;box-sizing:border-box;background:\1;"
        r"display:flex;flex-direction:column",
        html, f"{name}: outer carpet", required=False)

    # 2. The device mockup: a fixed 390x844 slab with a border, 42px radius and
    #    a drop shadow. Becomes the viewport itself.
    html = _sub_once(
        r"width:390px;height:844px;flex:none;position:relative;background:(#[0-9a-fA-F]{3,8});"
        r"border:1px solid #[0-9a-fA-F]{3,8};border-radius:42px;",
        r"width:100%;flex:1;min-height:100vh;position:relative;background:\1;"
        r"border:none;border-radius:0;",
        html, f"{name}: phone mockup", required=False)

    # 3. The desktop console's equivalent: a fixed 1440x900 bordered slab.
    html = _sub_once(
        r"display:flex;width:1440px;height:900px;box-sizing:border-box;",
        r"display:flex;width:100%;min-height:100vh;box-sizing:border-box;",
        html, f"{name}: desktop mockup", required=False)

    # 4. Anything left over from the mockup: its shadow and its rounding.
    html = html.replace("box-shadow:0 40px 110px rgba(0,0,0,.6)", "")
    html = html.replace("box-shadow:0 40px 120px rgba(0,0,0,.7)", "")

    # 5. The caption naming the viewport ("... · IPHONE 390").
    html = _sub_once(
        r"<div style=\"[^\"]*\">[^<]*·[^<]*IPHONE[^<]*</div>\s*", "",
        html, f"{name}: viewport caption", required=False)

    # 6. The painted-on status bar. A real device draws its own, and two
    #    clocks disagreeing is worse than none.
    #    Anchored on the bar's own signature (fixed height, space-between, the
    #    mono face) plus a clock as its first child, because the battery half
    #    differs between designs -- one is a bare span, the other nests a drawn
    #    battery. Neither contains a nested <div>, so the first closing tag
    #    ends the bar.
    html = _sub_once(
        r"<div style=\"flex:none;height:4\d+px;display:flex;align-items:center;"
        r"justify-content:space-between;padding:0 26px;font-family:'JetBrains Mono'[^\"]*\">"
        r"\s*<span>\d{2}:\d{2}</span>.*?</div>\s*",
        "", html, f"{name}: painted status bar", required=False)

    # 7. The trailing hint addressed to someone reviewing the design.
    html = _sub_once(
        r"<div style=\"[^\"]*\">\s*Tap a graph node to re-centre[^<]*</div>\s*",
        "", html, f"{name}: design-review caption", required=False)

    verify_frame_removed(html, name)
    return html


# Signatures of the design mockup that must not survive into a shipped build.
# Each is specific to the frame rather than to page content -- a 42px radius
# on a full-screen slab, a hard 844px height, the viewport caption.
_FRAME_MARKERS = (
    ("844px", "the device mockup's fixed height"),
    ("border-radius:42px", "the device mockup's rounded corners"),
    ("IPHONE", "the viewport caption"),
    ("box-shadow:0 40px 110px", "the device mockup's drop shadow"),
    ("box-shadow:0 40px 120px", "the device mockup's drop shadow"),
)


class FrameNotStripped(RuntimeError):
    """A device-frame signature survived the strip pass."""


def verify_frame_removed(html: str, name: str) -> None:
    """
    Fail the build if any frame signature survives.

    Every substitution in `strip_device_frame` is `required=False`, because a
    given design file legitimately may not contain every element. The cost of
    that tolerance is silence: if a design is restyled so a pattern no longer
    matches, the strip quietly does nothing and the app ships with a picture
    of a phone drawn inside the phone, complete with a second status bar.

    That is exactly what happened, and it was diagnosed from a screenshot
    rather than from the build. So the build now checks its own output and
    refuses rather than warning -- a warning in a hundred lines of build log
    is a warning nobody reads.
    """
    found = [(marker, why) for marker, why in _FRAME_MARKERS if marker in html]
    if not found:
        return
    detail = "; ".join(f"{marker!r} ({why})" for marker, why in found)
    raise FrameNotStripped(
        f"{name}: the device mockup was not fully removed -- {detail}. The design file has "
        "probably been restyled so a pattern in strip_device_frame no longer matches. Fix "
        "the pattern; do not ship the frame.")


def _status_bar_present(html: str) -> bool:
    """A painted-on clock is the most visible half of the frame."""
    import re as _re
    return bool(_re.search(r"<span>\d{2}:\d{2}</span>", html))


def _inline_js(js: str, label: str) -> str:
    """
    Emit JavaScript as a <script> block that no HTML parser can truncate.

    Escaping `</script` alone is not enough. The parser has three script
    states, and `<!--` inside script text moves it into the escaped state
    where a later `<script` moves it into the *double* escaped state -- in
    which `</script>` no longer closes the element. Between them, these files
    contain every ingredient: `quintek-eval-api.js` documents its backend seam
    with a literal `<script>...</script>` in a comment, `babel.min.js` carries
    an `<!--`, and `support.js` mentions `<script data-dc-script>` in an error
    string. The failure is silent and spectacular: the tag ends early and the
    rest of the library renders on the page as visible source text.

    Base64 contains no `<` at all, so the parser has nothing to react to. The
    payload is decoded and run through indirect eval, which evaluates in
    global scope -- `support.js` installs globals and would break under a
    direct eval's local scope.
    """
    payload = base64.b64encode(js.encode("utf-8")).decode("ascii")
    return (
        f"<script>\n/* ---- inlined: {label} (base64 to survive HTML parsing) ---- */\n"
        f'(0,eval)(new TextDecoder().decode(Uint8Array.from(atob("{payload}"),'
        f" c => c.charCodeAt(0))));\n</script>\n"
    )


def _module_to_iife(source: str, path: Path) -> str:
    """
    Turn an ES module into an async IIFE resolving to its export object.

    `export const x = ...` becomes `const x = ...` plus an entry in the
    returned object. Module-level `await` is preserved because the IIFE is
    itself async -- which matters, since quintek-eval-api.js awaits its
    backend fetch at load time so consumers can read `api.candidates`
    synchronously once the promise settles.
    """
    names: list[str] = []

    def strip_export(match: re.Match) -> str:
        kind, name = match.group(1), match.group(2)
        names.append(name)
        return f"{kind} {name}"

    # `async function` and `function*` are as much an export form as `const`;
    # missing them is what the leftover check below exists to catch.
    body = re.sub(
        r"\bexport\s+((?:async\s+)?function\s*\*?|const|let|var|class)\s+([A-Za-z_$][\w$]*)",
        strip_export, source)

    # Only a real statement counts -- the word "export" appears in these
    # modules' own prose comments, and tripping on that would block a bundle
    # that is actually fine.
    leftover = [ln for ln in body.splitlines()
                if re.match(r"\s*export\b", ln) and not ln.lstrip().startswith(("//", "*", "/*"))]
    if leftover:
        raise SystemExit(
            f"{path.name}: unhandled export form(s), refusing to emit a bundle "
            f"that would silently drop them:\n  " + "\n  ".join(leftover[:3]))

    exports = ", ".join(f"{n}: {n}" for n in sorted(set(names)))
    return (
        f"/* ---- inlined module: {path.name} ---- */\n"
        f"window.__DC_MOD[{path.name!r}] = (async () => {{\n"
        f"{body}\n"
        f"return {{ {exports} }};\n"
        f"}})();\n"
    )


def _inline_scripts(html: str, base: Path) -> str:
    """Replace <script src="./local.js"></script> with the file's contents."""
    def repl(match: re.Match) -> str:
        src = match.group(1)
        if src.startswith(("http://", "https://", "//")):
            return match.group(0)
        target = (base / src.lstrip("./")).resolve()
        if not target.exists():
            raise SystemExit(f"missing local script referenced by the page: {src}")
        return _inline_js(target.read_text(encoding="utf-8"), target.name)

    return re.sub(r'<script[^>]*\ssrc="([^"]+)"[^>]*>\s*</script>', repl, html)


def build(key: str, *, keep_frame: bool = False) -> Path:
    filename, title = TARGETS[key]
    source_path = FRONTEND / filename
    if not source_path.exists():
        raise SystemExit(f"no such source: {source_path}")

    html = source_path.read_text(encoding="utf-8")
    if not keep_frame:
        html = strip_device_frame(html, key)

    react = (VENDOR / "react.production.min.js").read_text(encoding="utf-8")
    react_dom = (VENDOR / "react-dom.production.min.js").read_text(encoding="utf-8")
    # `support.js` transpiles each <script type="text/x-dc"> component with
    # Babel, fetched from unpkg by `ensureBabel()`. Without it the component
    # source is evaluated raw and dies on the first class field with
    # "Missing initializer in const declaration" -- the page then renders its
    # chrome but no application. `ensureBabel()` short-circuits on
    # `window.Babel`, so inlining it here is enough.
    babel = (VENDOR / "babel.min.js").read_text(encoding="utf-8")

    modules = discover_modules(html, FRONTEND)
    module_block = "window.__DC_MOD = {};\n" + "\n".join(
        _module_to_iife((FRONTEND / m).read_text(encoding="utf-8"), FRONTEND / m)
        for m in modules
    )

    # Dynamic imports resolve to the pre-evaluated promises above.
    for m in modules:
        html = html.replace(f"import('./{m}')", f"window.__DC_MOD['{m}']")
        html = html.replace(f'import("./{m}")', f"window.__DC_MOD['{m}']")

    html = _inline_scripts(html, FRONTEND)

    # Nothing may survive as a live dynamic import: a single one left behind is
    # a feature that works in the browser and is absent from the APK.
    leftover = IMPORT_RE.findall(html)
    if leftover:
        raise MissingModule(
            "dynamic imports survived the build: " + ", ".join(sorted(set(leftover)))
            + ". They would reject at runtime and the app would report itself"
              " unconfigured rather than broken.")

    preamble = (
        _inline_js(react, "react 18.3.1 UMD")
        + _inline_js(react_dom, "react-dom 18.3.1 UMD")
        + _inline_js(babel, "@babel/standalone 7.29.0")
        + _inline_js(module_block, "quintek API modules")
    )

    # The page is emitted as artifact-ready content: no <!doctype>, <html>,
    # <head> or <body> of its own, since the host supplies that skeleton.
    html = re.sub(r"<!DOCTYPE[^>]*>", "", html, flags=re.I)
    head = re.search(r"<head[^>]*>(.*?)</head>", html, re.S | re.I)
    body = re.search(r"<body[^>]*>(.*?)</body>", html, re.S | re.I)
    inner = ((head.group(1) if head else "") + (body.group(1) if body else "")) or html
    inner = re.sub(r"</?(html|head|body)[^>]*>", "", inner, flags=re.I)
    inner = re.sub(r"<title>.*?</title>", "", inner, flags=re.S | re.I)

    DIST.mkdir(parents=True, exist_ok=True)
    # "PG Revision.dc.html" -> "pg-revision.html". The `.dc` marks a design
    # -component source; the built artefact is a plain page, and the Android
    # asset names in Screens.kt depend on this exact form.
    slug = Path(filename).stem.removesuffix(".dc").replace(" ", "-").lower()
    out = DIST / f"{slug}.html"
    page = f"<title>{title}</title>\n{preamble}{inner}\n"
    out.write_text(page, encoding="utf-8")

    # The Android app serves these same bundles out of its APK. Writing both
    # from one build keeps the phone from quietly running an older screen than
    # the browser -- the failure mode is invisible until someone compares two
    # numbers that should match.
    if out.name in ANDROID_SCREENS:
        ANDROID_ASSETS.mkdir(parents=True, exist_ok=True)
        (ANDROID_ASSETS / out.name).write_text(page, encoding="utf-8")
    return out


def main() -> None:
    args = sys.argv[1:]
    # The design files are previews of a phone sitting on a dark backdrop.
    # Builds strip that chrome by default, because every consumer here -- the
    # Android app, a page opened on a real phone -- wants the application, not
    # a picture of one. --keep-frame preserves it for design review.
    keep_frame = "--keep-frame" in args
    wanted = [a for a in args if not a.startswith("--")] or list(TARGETS)
    for key in wanted:
        if key not in TARGETS:
            raise SystemExit(f"unknown target {key!r}; choose from {list(TARGETS)}")
        out = build(key, keep_frame=keep_frame)
        print(f"{key:<16} -> {out.relative_to(ROOT)}  ({out.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
