"""
The guard that catches the NEXT retirement.

Everything else in this repository responds to the 2026-08-26 NVIDIA
retirement after the fact: the registry records it, the router excludes it,
the orchestrator breaks the circuit, the experiment refuses to start. None of
that helps with the actual root cause, which is that a model id written into
Python source is a claim nobody re-checks.

So this file is the re-check. It walks the repository, finds every literal
model id in code that could reach a provider, and fails if one of them is a
model the discovery registry has observed to be gone.

EXEMPTIONS, AND WHY EACH IS LEGITIMATE
--------------------------------------
A hard-coded model id is fine where it is a RECORD of something rather than an
INSTRUCTION to do something:

  * `discovery/`   observations, including the retirement itself. A file whose
                   job is to say "this model is dead" obviously names it.
  * `reports/`, `runs/`   immutable run records. A benchmark result that could
                   not name the model it measured would be unauditable.
  * `docs/`        prose, including the incident write-ups.
  * `tests/`       fixtures. A test for retirement handling needs a retired id.
  * docstrings and comments   explanation, not configuration. Checked
                   separately and reported, but not failed, because
                   `_infer_family`'s docstring legitimately shows worked
                   examples.

What is NOT exempt is a string literal in a module or tool that could become a
request: a default argument, a dict of per-provider models, a spec. Those are
the ones that went stale silently, and those are what this fails on.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from benchmark.discovery import (Availability, DEFAULT_REGISTRY_PATH,
                                 DynamicModelRegistry, ROLE_REQUIREMENTS)

ROOT = Path(__file__).resolve().parent.parent

#: Directories whose contents are records, prose or fixtures rather than
#: instructions. See the module docstring for why each one is here.
EXEMPT_DIRS = {"tests", "docs", "discovery", "reports", "runs", "corpus",
               "alpha0_runs", "generation_run", ".git", "__pycache__",
               "frontend", "android", "node_modules"}

#: Files that exist to describe the incident or seed a historical fixture.
EXEMPT_FILES = {"tools_make_synthetic.py"}


def python_sources() -> list[Path]:
    out = []
    for path in ROOT.rglob("*.py"):
        parts = set(path.relative_to(ROOT).parts)
        if parts & EXEMPT_DIRS or path.name in EXEMPT_FILES:
            continue
        out.append(path)
    return sorted(out)


def string_literals(path: Path) -> list[tuple[int, str]]:
    """
    Every string literal in a module, EXCLUDING docstrings.

    A docstring is documentation; a literal in an argument default or a dict is
    configuration. Only the second can be handed to a provider, and conflating
    them would fail this repository for explaining its own incident.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None and node.body:
                first = node.body[0]
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                    docstrings.add(id(first.value))
    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstrings):
            out.append((getattr(node, "lineno", 0), node.value))
    return out


@pytest.fixture(scope="module")
def registry():
    path = Path(DEFAULT_REGISTRY_PATH)
    if not path.exists():
        pytest.skip("no discovery registry on disk; run tools_discovery.py first")
    return DynamicModelRegistry(path)


def test_no_live_code_path_names_a_retired_model(registry):
    """
    The one that would have caught this incident on 2026-08-26 instead of two
    days later, and will catch the next one on the day discovery notices.
    """
    dead = {r.model_id: r for r in registry.retired() if r.model_id}
    if not dead:
        pytest.skip("the registry knows of no retired models yet")

    offences = []
    for path in python_sources():
        for lineno, literal in string_literals(path):
            for model_id, record in dead.items():
                if model_id in literal:
                    offences.append(
                        f"{path.relative_to(ROOT)}:{lineno} names {model_id!r}, "
                        f"which is RETIRED ({record.retirement_reason[:70]})")
    assert not offences, (
        "a live code path names a model the provider has withdrawn:\n  "
        + "\n  ".join(offences)
        + "\n\nResolve the model from benchmark.discovery at run time instead of "
          "writing an id into source. If this literal is genuinely a record or a "
          "fixture rather than something that could become a request, move it "
          "into one of the exempt locations named in this file's docstring.")


def test_no_live_code_path_names_a_model_that_stopped_serving(registry):
    """
    Weaker than retirement and worth knowing: a 404 is an entitlement change,
    reversible, so this reports rather than fails on the count -- but a
    DEFAULT argument naming one is still a tool that cannot run.
    """
    not_serving = {r.model_id for r in registry.all()
                   if r.availability == Availability.NOT_SERVING and r.model_id}
    found = []
    for path in python_sources():
        for lineno, literal in string_literals(path):
            for model_id in not_serving:
                if model_id in literal:
                    found.append(f"{path.relative_to(ROOT)}:{lineno} -> {model_id}")
    assert not found, (
        "a live code path names a model this account cannot currently call:\n  "
        + "\n  ".join(found))


def test_the_committed_candidate_registry_contains_no_retired_model(registry):
    """
    `configs/model_registry.json` is what `benchmark/router.py` selects
    production traffic from. Four of its five seeded models were dead when
    this was written, which is the whole reason the seeding tool now derives
    from discovery.
    """
    path = ROOT / "configs" / "model_registry.json"
    if not path.exists():
        pytest.skip("no candidate registry committed")
    raw = json.loads(path.read_text(encoding="utf-8"))
    dead = {r.provider + ":" + r.model_id for r in registry.retired()}
    # DEPRECATED and FAILED are terminal and unselectable. A retired model MUST
    # still appear here in one of them -- deleting the row would lose the
    # answer to "what was registered in August, and why is it not any more",
    # and a benchmark result naming a candidate the registry has forgotten is
    # unauditable. What must not happen is a retired model sitting in a status
    # the router or an evaluation run can still pick up.
    selectable = [f"{c['candidate_id']} -> {c['provider']}:{c['model_id']} "
                  f"is {c['status']}"
                  for c in raw.values()
                  if f"{c['provider']}:{c['model_id']}" in dead
                  and c["status"] not in ("DEPRECATED", "FAILED")]
    assert not selectable, (
        "the production candidate registry offers retired models:\n  "
        + "\n  ".join(selectable)
        + "\n\nRetire them: python3 tools_seed_model_registry.py --deprecate-retired")


def test_a_deprecated_candidate_keeps_its_row_rather_than_being_deleted(registry):
    """
    The other half of the rule above. Deprecation is an exit, not an erasure.
    """
    path = ROOT / "configs" / "model_registry.json"
    if not path.exists():
        pytest.skip("no candidate registry committed")
    raw = json.loads(path.read_text(encoding="utf-8"))
    dead = {r.provider + ":" + r.model_id for r in registry.retired()}
    remembered = [c for c in raw.values()
                  if f"{c['provider']}:{c['model_id']}" in dead]
    if not remembered:
        pytest.skip("no seeded candidate has since been retired")
    for candidate in remembered:
        assert candidate["status"] == "DEPRECATED"
        assert candidate["created_at"], "the registration date must survive"


def test_roles_are_defined_in_exactly_one_place():
    """
    There were two role tables briefly -- one in `benchmark/candidates.py` as
    `ROLE_FILTERS`, one in `tools_discovery.py`. Two definitions of what
    "validation" requires is two answers to the only question that matters.
    """
    import tools_discovery

    assert tools_discovery.ROLE_REQUIREMENTS is ROLE_REQUIREMENTS
    duplicates = []
    for path in python_sources():
        text = path.read_text(encoding="utf-8")
        if "ROLE_REQUIREMENTS = {" in text and path.name != "discovery.py":
            duplicates.append(str(path.relative_to(ROOT)))
    assert not duplicates, f"ROLE_REQUIREMENTS is redefined in {duplicates}"


def test_no_tool_that_spends_credits_carries_a_literal_model_default():
    """
    The specific shape that failed: `parser.add_argument("--validator",
    default="meta/llama-3.1-8b-instruct")`. A default is the value most runs
    actually use, so a stale one is not an edge case -- it is the main path.
    """
    offences = []
    for path in sorted(ROOT.glob("tools_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and getattr(node.func, "attr", "") == "add_argument"):
                continue
            for keyword in node.keywords:
                if keyword.arg != "default":
                    continue
                if not (isinstance(keyword.value, ast.Constant)
                        and isinstance(keyword.value.value, str)):
                    continue
                value = keyword.value.value
                # A model id on these providers is namespaced with a slash --
                # and so is a repository path. `--corpus corpus/validator_dev`
                # is not a model, and the cheapest way to know that is that it
                # exists on disk. Anything slash-namespaced that is NOT a path
                # in this repository is the shape that went stale.
                if not ("/" in value and " " not in value
                        and not value.startswith(("/", "./", "http"))
                        and not value.endswith("/")):
                    continue
                if (ROOT / value).exists():
                    continue
                offences.append(
                    f"{path.name}:{node.lineno} --{_flag_of(node)} "
                    f"defaults to {value!r}")
    assert not offences, (
        "a CLI default looks like a hard-coded model id:\n  " + "\n  ".join(offences)
        + "\n\nResolve it from benchmark.discovery at run time; an unset default "
          "that refuses is better than a stale one that spends.")


def _flag_of(node: ast.Call) -> str:
    for arg in node.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value.lstrip("-")
    return "?"
