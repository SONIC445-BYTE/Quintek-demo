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

# The .dc.html sources to build, and the title each standalone should carry.
TARGETS = {
    "PG Revision": ("PG Revision.dc.html", "Quintek PG Revision"),
    "Quintek Admin": ("Quintek Admin.dc.html", "Quintek Admin Console"),
    "Quintek Harness": ("Quintek Harness.dc.html", "Quintek Harness"),
    "Quintek Audit": ("Quintek Audit.dc.html", "Quintek Audit"),
}

# ES modules the .dc.html files import at runtime, in dependency order.
MODULES = ["quintek-eval-api.js", "quintek-report-api.js"]


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


def build(key: str) -> Path:
    filename, title = TARGETS[key]
    source_path = FRONTEND / filename
    if not source_path.exists():
        raise SystemExit(f"no such source: {source_path}")

    html = source_path.read_text(encoding="utf-8")

    react = (VENDOR / "react.production.min.js").read_text(encoding="utf-8")
    react_dom = (VENDOR / "react-dom.production.min.js").read_text(encoding="utf-8")
    # `support.js` transpiles each <script type="text/x-dc"> component with
    # Babel, fetched from unpkg by `ensureBabel()`. Without it the component
    # source is evaluated raw and dies on the first class field with
    # "Missing initializer in const declaration" -- the page then renders its
    # chrome but no application. `ensureBabel()` short-circuits on
    # `window.Babel`, so inlining it here is enough.
    babel = (VENDOR / "babel.min.js").read_text(encoding="utf-8")

    module_block = "window.__DC_MOD = {};\n" + "\n".join(
        _module_to_iife((FRONTEND / m).read_text(encoding="utf-8"), FRONTEND / m)
        for m in MODULES
    )

    # Dynamic imports resolve to the pre-evaluated promises above.
    for m in MODULES:
        html = html.replace(f"import('./{m}')", f"window.__DC_MOD['{m}']")
        html = html.replace(f'import("./{m}")', f"window.__DC_MOD['{m}']")

    html = _inline_scripts(html, FRONTEND)

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
    out = DIST / (Path(filename).stem.replace(" ", "-").lower() + ".html")
    out.write_text(f"<title>{title}</title>\n{preamble}{inner}\n", encoding="utf-8")
    return out


def main() -> None:
    wanted = sys.argv[1:] or list(TARGETS)
    for key in wanted:
        if key not in TARGETS:
            raise SystemExit(f"unknown target {key!r}; choose from {list(TARGETS)}")
        out = build(key)
        print(f"{key:<16} -> {out.relative_to(ROOT)}  ({out.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
