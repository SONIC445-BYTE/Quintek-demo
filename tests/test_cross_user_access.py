"""
Direct-object access, as a class rather than as a list of cases.

A second learner, authenticated as themselves, aims every id-taking route at
the first learner's ids. The assertion is NOT a status code -- it is that no
response to B contains any of A's content.

That distinction is the whole point of this file. The `concept_ids` disclosure
returned **201 Created**, a completely correct status, while writing A's
confidential source text into B's questions. A status-only test would have
passed it. So would a test that only checked list endpoints, which is what the
suite had: 1145 tests were green while `POST /attempts` handed any learner
another learner's uploaded document.

Three guards keep this from decaying into a list that someone forgets to
extend:

  * the route inventory is DERIVED from `student/api.py` by parsing it, not
    retyped here, so a new id-taking route is discovered rather than declared;
  * a guard shape the parser does not understand is a FAILURE, not a skip --
    an unrecognised route must never be silently uncovered;
  * ids arrive in bodies as well as paths (`/attempts` did), so `body.get`
    calls for `*_id` fields are inventoried too.

And every content assertion is paired with a POSITIVE CONTROL: A's own request
must return the sentinel. Without that, a mistyped sentinel makes every
negative assertion pass while proving nothing.
"""

from __future__ import annotations

import ast
import json
import pathlib
import sqlite3
import subprocess
import sys

import pytest

from student.ai import AIEngine
from student.api import StudentAPI
from student.db import Database, now_iso
from student.generation import AIConceptExtractor, QuestionGenerator
from student.ingestion import IngestionEngine
from student.notifications import NotificationService
from student.validation import QuestionValidator

from test_student_e2e import _Scripted, _reply_for

ROOT = pathlib.Path(__file__).resolve().parent.parent
API_PATH = ROOT / "student" / "api.py"


# ---------------------------------------------------------------------------
# Deriving the route inventory from the source
# ---------------------------------------------------------------------------

class _UnparsedGuard(Exception):
    """A condition on `seg` that the extractor does not understand.

    Raised rather than skipped. A guard shape nobody anticipated is exactly the
    case where a route could slip through uncovered.
    """


def _parse_guard(test) -> tuple[list | None, set, list]:
    """`(shape, methods, seg_fragments_not_understood)` for one `if` test.

    `shape` is a list of segments; `None` marks a slot filled by a client id.
    """
    shape: dict[int, str] = {}
    methods: set[str] = set()
    unparsed: list[str] = []
    length: int | None = None

    def mentions_seg(node) -> bool:
        return any(isinstance(n, ast.Name) and n.id == "seg" for n in ast.walk(node))

    def walk(n):
        nonlocal length
        if isinstance(n, ast.BoolOp) and isinstance(n.op, ast.And):
            for v in n.values:
                walk(v)
            return
        if isinstance(n, ast.Compare) and len(n.ops) == 1 and isinstance(n.ops[0], ast.Eq):
            left, right = n.left, n.comparators[0]
            if isinstance(left, ast.Name) and left.id == "method":
                methods.add(ast.literal_eval(right))
                return
            if (isinstance(left, ast.Call) and isinstance(left.func, ast.Name)
                    and left.func.id == "len" and left.args
                    and isinstance(left.args[0], ast.Name) and left.args[0].id == "seg"):
                length = ast.literal_eval(right)
                return
            if isinstance(left, ast.Subscript) and isinstance(left.value, ast.Name) \
                    and left.value.id == "seg":
                sl = left.slice
                if isinstance(sl, ast.Constant):
                    shape[sl.value] = ast.literal_eval(right)
                    return
                if isinstance(sl, ast.Slice):
                    start = sl.lower.value if sl.lower else 0
                    for j, v in enumerate(ast.literal_eval(right)):
                        shape[start + j] = v
                    return
            if isinstance(left, ast.Name) and left.id == "seg":
                vals = ast.literal_eval(right)
                for j, v in enumerate(vals):
                    shape[j] = v
                length = len(vals)
                return
        if isinstance(n, ast.Name) and n.id == "seg":
            return                      # a bare `seg and ...` truthiness check
        if mentions_seg(n):
            unparsed.append(ast.dump(n)[:120])

    walk(test)
    if length is None:
        return None, methods, unparsed
    return [shape.get(i) for i in range(length)], methods, unparsed


def route_inventory() -> tuple[set[tuple], list[str]]:
    tree = ast.parse(API_PATH.read_text(encoding="utf-8"))
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "StudentAPI")
    fns = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}

    routes: set[tuple] = set()
    unparsed: list[str] = []
    for name, prefix in (("_route", ()), ("_ai", ("ai",)), ("_auth", ("auth",))):
        for node in ast.walk(fns[name]):
            if not isinstance(node, ast.If):
                continue
            shape, _methods, unk = _parse_guard(node.test)
            unparsed.extend(unk)
            if shape is not None:
                routes.add(tuple(prefix) + tuple(shape))
    return routes, unparsed


#: Body fields that carry the CALLER'S OWN CONTENT -- prose, numbers, settings,
#: a credential they are supplying. None of them names something the server
#: already owns, so none needs cross-user coverage.
#:
#: This list is the whole predicate, and it is deny-by-default ON PURPOSE.
#:
#: It used to be the other way round: a field counted as an object reference
#: only if its name ended in `_id` or `_ids`. `storage_key` ends in neither, so
#: `add_source` was never discovered, never required to declare coverage, and
#: shipped a field that let one learner name another learner's stored file and
#: read its contents. That is the SECOND time a discovery predicate has been
#: the gap rather than the coverage table -- the first was the path-shape sweep
#: that missed `/attempts` taking its id from the body.
#:
#: An allow-list of names to SUSPECT can only ever catch the naming conventions
#: someone thought of. An allow-list of names to EXCUSE fails closed: a new
#: field nobody classified breaks this test until a human says which it is.
CONTENT_FIELDS = frozenset({
    # auth and profile: the caller's own details
    "email", "name", "password", "timezone",
    # notifications
    "email_enabled", "push_enabled", "trigger_time", "note_text",
    # notebooks and sources: the material being contributed
    "title", "subject", "kind", "filename", "mime_type", "text", "url",
    "content_base64",
    # generation parameters: knobs, not references
    "count", "validate", "family", "difficulty", "reasoning_depth", "constraints",
    # demo authoring: the caller's own worked example
    "question", "question_type", "question_target", "answer_format",
    "distractor_strategy", "stem_structure", "notes",
    # revision
    "strategy", "selected_question_count",
    # attempt payload: what the learner did, not what they are pointing at
    "user_answer", "user_colour", "colour", "gaps",
    # reporting a question: the learner's own words about what is wrong with
    # it. `kind` is above already; `note` is free text and names nothing.
    "note",
    # an administrator's stated reason for suspending an account. Their own
    # words, recorded so the suspended person can be told why.
    "reason",
    # closing a report. `resolution` is checked against a fixed set in
    # safety.resolve and reaches no row lookup; `resolved_by` is the NAME of
    # whoever made the call, free text, recorded so an anonymous resolution is
    # impossible. Neither points at a server-owned object -- the report itself
    # is named in the path, and that id IS covered, as /ops/reports/<id>.
    "resolution", "resolved_by",
})


def body_fields_read() -> dict[str, set[str]]:
    """`handler name -> {every body field it reads}`. No filtering."""
    tree = ast.parse(API_PATH.read_text(encoding="utf-8"))
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "StudentAPI")
    found: dict[str, set[str]] = {}
    for fn in (n for n in cls.body if isinstance(n, ast.FunctionDef)):
        hits = {
            n.args[0].value
            for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "get" and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "body" and n.args
            and isinstance(n.args[0], ast.Constant)
            and isinstance(n.args[0].value, str)
        }
        if hits:
            found[fn.name] = hits
    return found


def body_id_fields() -> dict[str, set[str]]:
    """
    `handler name -> {body fields that name something the SERVER owns}`.

    Everything that is not declared caller content. See `CONTENT_FIELDS`.
    """
    return {handler: refs
            for handler, fields in body_fields_read().items()
            if (refs := fields - CONTENT_FIELDS)}


def _key(shape: tuple) -> str:
    return "/" + "/".join(s if s else "<id>" for s in shape)


# ---------------------------------------------------------------------------
# What each id-taking route is expected to do with a stranger's id
# ---------------------------------------------------------------------------
#
# `id_kind` names which of A's ids to substitute. `expect` is the CONTRACT:
#
#   REFUSED  -- must answer 401/403/404. The resource is owned.
#   SCOPED   -- may answer 200, because the route reads only the caller's own
#               rows regardless of the id handed to it. Still must not contain
#               A's content, which is what is actually asserted.
#
# Adding a route to `student/api.py` without adding it here fails
# `test_every_id_taking_route_is_covered`.

REFUSED, SCOPED = "REFUSED", "SCOPED"

COVERAGE: dict[str, dict] = {
    "/notebooks/<id>":                    dict(kind="notebook", method="GET", expect=REFUSED),
    "/notebooks/<id>/sources":            dict(kind="notebook", method="POST", expect=REFUSED,
                                               body={"kind": "text", "text": "x" * 400}),
    "/notebooks/<id>/questions":          dict(kind="notebook", method="GET", expect=REFUSED),
    "/sources/<id>/progress":             dict(kind="source", method="GET", expect=REFUSED),
    "/questions/<id>":                    dict(kind="question", method="GET", expect=REFUSED),
    # Reporting a question is a WRITE against someone else's row if the join is
    # missing, and the report would then name a question its author cannot see.
    "/questions/<id>/reports":            dict(kind="question", method="POST", expect=REFUSED,
                                               body={"kind": "factually_wrong"}),
    # Admin-only. A learner gets 404 from `_require_admin`, which is the same
    # refusal shape every owned resource here uses -- a 403 would confirm the
    # route exists and that the caller is merely the wrong person.
    "/admin/users/<id>":                  dict(kind="notebook", method="GET", expect=REFUSED),
    "/admin/users/<id>/<id>":             dict(kind="notebook", method="POST", expect=REFUSED,
                                               body={"reason": "test"}),
    # Resolving a report. Admin-only, so a learner -- including the one who
    # filed it -- gets 404 from `_require_admin`. That matters beyond the usual
    # ownership argument: a complainant who could mark their own report
    # "upheld" would make the resolution meaningless, and a content error
    # promoted to an adjudication candidate on nobody's judgement but the
    # reporter's is exactly the corpus contamination the gold pathway exists to
    # prevent.
    "/ops/reports/<id>":                  dict(kind="question", method="POST", expect=REFUSED,
                                               body={"resolution": "upheld"}),
    "/gaps/<id>":                         dict(kind="gap", method="GET", expect=SCOPED,
                                               why="joins knowledge_gaps on user_id, so a"
                                                   " stranger's id yields empty evidence"),
    "/gaps/<id>/questions":               dict(kind="gap", method="GET", expect=REFUSED),
    "/gaps/<id>/resolve":                 dict(kind="gap", method="POST", expect=SCOPED, body={},
                                               why="UPDATE ... WHERE id=? AND user_id=? matches"
                                                   " nothing; reports ok for a no-op"),
    "/revision/sessions/<id>/complete":   dict(kind="session", method="POST", expect=REFUSED,
                                               body={}),
    "/concepts/<id>":                     dict(kind="concept", method="GET", expect=SCOPED,
                                               why="concepts are a GLOBAL vocabulary by design;"
                                                   " the per-learner parts are owner-joined"),
    "/concepts/<id>/graph":               dict(kind="concept", method="GET", expect=SCOPED,
                                               why="ignores the id entirely and returns the"
                                                   " caller's own graph"),
    # Benchmark models, not learner data: no owner exists to scope against.
    "/ai/models/<id>":                    dict(kind=None, method="GET", expect=SCOPED,
                                               why="benchmark model id, not a learner resource"),
    "/ai/models/<id>/history":            dict(kind=None, method="GET", expect=SCOPED,
                                               why="benchmark model id, not a learner resource"),
}

# Body-borne ids, inventoried separately because `/attempts` proved a path-shape
# sweep is not enough. `checked` names the test that covers each field.
BODY_ID_COVERAGE: dict[tuple[str, str], str] = {
    ("record_attempt", "question_id"):     "test_attempts_refuses_another_learners_question",
    ("record_attempt", "session_id"):      "test_attempts_refuses_another_learners_question",
    ("generate_questions", "source_id"):   "test_generation_cannot_be_grounded_in_another_learner",
    ("generate_questions", "concept_ids"): "test_generation_cannot_be_grounded_in_another_learner",
    ("generate_questions", "demo_ids"):    "test_generation_cannot_be_grounded_in_another_learner",
    # Billing labels rather than access filters: they name which reservation the
    # spend belongs to. Nothing is read by them.
    ("generate_questions", "batch_id"):       "not-an-access-filter",
    ("generate_questions", "reservation_id"): "not-an-access-filter",
}


# ---------------------------------------------------------------------------
# Two learners, one of whom has something worth stealing
# ---------------------------------------------------------------------------

SENTINEL = "ZZ-CONFIDENTIAL-WARD-NOTE-ZZ"
A_TEXT = (f"{SENTINEL}. Pre-renal acute kidney injury arises from hypoperfusion. The "
          "fractional excretion of sodium is below one percent in pre-renal disease, "
          "whereas intrinsic renal injury shows values above two percent. " * 4)
# B studies the SAME TOPIC from their own material. This is what makes the
# concept_ids case reachable without any borrowed identifier.
B_TEXT = ("Pre-renal acute kidney injury arises from hypoperfusion. The fractional "
          "excretion of sodium is below one percent in pre-renal disease, whereas "
          "intrinsic renal injury shows values above two percent. " * 4)


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("crossuser")
    db = Database(tmp / "q.db")
    provider = _Scripted(_reply_for)
    ai = AIEngine(db, provider_factory=lambda c: provider, development_candidate="cand-gen")
    engine = IngestionEngine(db, concept_extractor=AIConceptExtractor(db, ai),
                             storage_dir=tmp / "src")
    validator_ai = AIEngine(db, provider_factory=lambda c: provider,
                            development_candidate="cand-val")
    api = StudentAPI(db, engine=engine, ai=ai, generator=QuestionGenerator(db, ai),
                     validator=QuestionValidator(db, validator_ai),
                     notifier=NotificationService(db, sender=lambda p: True))

    def register(email):
        return api.handle("POST", "/auth/register", {},
                          {"email": email, "password": "correct-horse"}, None)[1]["token"]

    a_token, b_token = register("a@example.com"), register("b@example.com")

    _, nb_a = api.handle("POST", "/notebooks", {},
                         {"title": "A private", "subject": "Medicine"}, a_token)
    _, src_a = api.handle("POST", f"/notebooks/{nb_a['id']}/sources", {},
                          {"kind": "text", "text": A_TEXT}, a_token)
    assert engine.wait_idle(60)
    api.handle("POST", f"/notebooks/{nb_a['id']}/questions", {},
               {"count": 4, "difficulty": "postgraduate"}, a_token)
    _, bank = api.handle("GET", f"/notebooks/{nb_a['id']}/questions", {}, {}, a_token)
    _, ses_a = api.handle("POST", "/revision/sessions", {}, {"count": 2}, a_token)
    _, served = api.handle("GET", "/revision/next", {"session": ses_a["session_id"]}, {}, a_token)
    api.handle("POST", "/attempts", {},
               {"question_id": served["question"]["question_id"], "user_answer": 1,
                "user_colour": "RED", "session_id": ses_a["session_id"],
                "gaps": ["FeNa interpretation"]}, a_token)
    _, gaps_a = api.handle("GET", "/gaps", {}, {}, a_token)
    _, cons_a = api.handle("GET", "/concepts", {}, {}, a_token)
    _, demo_a = api.handle("POST", "/demos", {},
                           {"title": "A demo", "question": f"{SENTINEL} demo stem?"}, a_token)

    _, nb_b = api.handle("POST", "/notebooks", {},
                         {"title": "B book", "subject": "Medicine"}, b_token)
    api.handle("POST", f"/notebooks/{nb_b['id']}/sources", {},
               {"kind": "text", "text": B_TEXT}, b_token)
    assert engine.wait_idle(60)
    engine.stop()

    yield {
        "api": api, "db": db, "provider": provider,
        "a": a_token, "b": b_token,
        "ids": {"notebook": nb_a["id"], "source": src_a["source_id"],
                "question": bank["questions"][0]["id"],
                "session": ses_a["session_id"], "gap": gaps_a["gaps"][0]["id"],
                "concept": cons_a["concepts"][0]["concept_id"], "demo": demo_a["id"]},
        "b_notebook": nb_b["id"],
    }


def _leaks(payload) -> bool:
    return SENTINEL in json.dumps(payload, default=str)


# ---------------------------------------------------------------------------
# The guards: the inventory must stay complete
# ---------------------------------------------------------------------------

def test_no_route_guard_is_unrecognised():
    """An `if` on `seg` the extractor cannot read is a hole, not a curiosity."""
    _routes, unparsed = route_inventory()
    assert not unparsed, (
        "student/api.py has route conditions this file cannot parse, so the coverage "
        "guard below cannot see them:\n  " + "\n  ".join(unparsed))


def test_every_id_taking_route_is_covered():
    routes, _ = route_inventory()
    id_routes = {_key(s) for s in routes if any(seg is None for seg in s)}
    missing = id_routes - set(COVERAGE)
    assert not missing, (
        "these routes take a client-supplied id and no cross-user expectation is "
        "declared for them in COVERAGE:\n  " + "\n  ".join(sorted(missing)) +
        "\n\nDeclare each as REFUSED (owned resource) or SCOPED (reads only the "
        "caller's rows, with a `why`). Do not delete this assertion.")
    stale = set(COVERAGE) - id_routes
    assert not stale, f"COVERAGE names routes that no longer exist: {sorted(stale)}"


def test_every_body_borne_id_is_covered():
    """`/attempts` took its id from the BODY. A path-shape sweep misses that."""
    missing = {
        (handler, field)
        for handler, fields in body_id_fields().items()
        for field in fields
        if (handler, field) not in BODY_ID_COVERAGE
    }
    assert not missing, (
        "these handlers read a body field that names something the SERVER owns, with "
        "no declared cross-user coverage:\n  "
        + "\n  ".join(f"{h}.{f}" for h, f in sorted(missing))
        + "\n\nIf the field carries the caller's own content, add it to CONTENT_FIELDS "
          "and say so. If it names a server-owned object, it needs a test proving one "
          "learner cannot use it to reach another's rows. `storage_key` looked like "
          "the former and was the latter.")


def test_scoped_routes_state_a_reason():
    for key, spec in COVERAGE.items():
        if spec["expect"] == SCOPED:
            assert spec.get("why"), (
                f"{key} is declared SCOPED, which permits a 200. Say why it cannot "
                "return another learner's rows, so the exemption is reviewable.")


# ---------------------------------------------------------------------------
# The positive control
# ---------------------------------------------------------------------------

def test_the_sentinel_is_actually_reachable(world):
    """Without this, a mistyped sentinel makes every assertion below vacuous."""
    api, ids = world["api"], world["ids"]
    status, payload = api.handle("GET", f"/questions/{ids['question']}", {}, {}, world["a"])
    assert status == 200
    assert _leaks(payload), (
        "A's own question does not carry the sentinel, so the cross-user assertions "
        "prove nothing. Fix the fixture before trusting this file.")


# ---------------------------------------------------------------------------
# The class itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", sorted(COVERAGE))
def test_no_id_taking_route_returns_another_learners_content(world, key):
    api, spec = world["api"], COVERAGE[key]
    kind = spec["kind"]
    if kind is None:
        pytest.skip(spec["why"])

    path = key.replace("<id>", world["ids"][kind])
    status, payload = api.handle(spec["method"], path, {}, spec.get("body"), world["b"])

    assert not _leaks(payload), (
        f"{spec['method']} {path} returned A's content to B:\n"
        f"{json.dumps(payload, default=str)[:400]}")

    if spec["expect"] == REFUSED:
        assert status in (401, 403, 404), (
            f"{spec['method']} {path} answered {status} for a resource B does not own; "
            f"every sibling route answers 404.")


def test_revision_next_refuses_another_learners_session(world):
    """Not in COVERAGE because the session id travels as a QUERY parameter."""
    api = world["api"]
    status, payload = api.handle("GET", "/revision/next",
                                 {"session": world["ids"]["session"]}, {}, world["b"])
    assert not _leaks(payload)
    assert status != 200, "B must not be served a question from A's session"


def test_attempts_refuses_another_learners_question(world):
    """The original defect: the id was in the body, and the reveal carried A's
    passage, the key and the rationale."""
    api = world["api"]
    status, payload = api.handle("POST", "/attempts", {},
                                 {"question_id": world["ids"]["question"], "user_answer": 1,
                                  "user_colour": "RED"}, world["b"])
    assert status == 404
    assert not _leaks(payload)
    assert "reveal" not in payload

    left = world["db"].query(
        "SELECT a.id FROM attempts a JOIN questions q ON q.id = a.question_id"
        " JOIN notebooks n ON n.id = q.primary_notebook_id"
        " WHERE a.user_id <> n.owner_id")
    assert left == [], "a refused attempt must leave no row behind"


def test_generation_cannot_be_grounded_in_another_learner(world):
    """
    The disclosure a status assertion would have missed: this returns 201.

    B generates in B's OWN notebook -- the ownership check passes correctly --
    and the leak rides in on `source_id`, `concept_ids` and `demo_ids`.
    """
    api, provider, ids = world["api"], world["provider"], world["ids"]
    _, own = api.handle("GET", "/concepts", {}, {}, world["b"])
    b_concepts = [c["concept_id"] for c in own["concepts"]]
    assert b_concepts, "B must have concepts of their own for this to be a real test"

    for label, body in [
        ("source_id", {"count": 2, "source_id": ids["source"]}),
        # No borrowed id at all: B's own concept ids, shared with A because the
        # concept vocabulary is global.
        ("concept_ids", {"count": 2, "concept_ids": b_concepts}),
        ("demo_ids", {"count": 1, "demo_ids": [ids["demo"]]}),
    ]:
        mark = len(provider.prompts)
        status, payload = api.handle(
            "POST", f"/notebooks/{world['b_notebook']}/questions", {}, body, world["b"])
        assert status in (201, 422), f"{label}: unexpected {status} {payload}"
        sent = "\n".join(provider.prompts[mark:])
        assert SENTINEL not in sent, (
            f"{label}: A's source text was put into a prompt for B's generation")

    _, bank = api.handle("GET", f"/notebooks/{world['b_notebook']}/questions", {}, {}, world["b"])
    for q in bank["questions"]:
        _, full = api.handle("GET", f"/questions/{q['id']}", {}, {}, world["b"])
        assert not _leaks(full), (
            f"question {q['id']} in B's notebook carries A's passage; the leak persists "
            "in storage even though the response that created it looked correct")


def test_no_cross_user_rows_exist_after_the_whole_exercise(world):
    """The invariant the remediation tool checks, asserted against a live database."""
    rows = world["db"].query(
        "SELECT q.id FROM questions q"
        " JOIN notebooks nq ON nq.id = q.primary_notebook_id"
        " JOIN sources s ON s.id = q.source_id"
        " JOIN notebooks ns ON ns.id = s.notebook_id"
        " WHERE nq.owner_id <> ns.owner_id")
    assert rows == [], f"{len(rows)} question(s) grounded in another learner's source"


# ---------------------------------------------------------------------------
# The helpers must refuse, not default
# ---------------------------------------------------------------------------

def test_passages_refuses_to_run_without_an_owner(world):
    gen = QuestionGenerator(world["db"], world["api"].ai)
    with pytest.raises(TypeError):
        gen._passages(source_id=None, concept_ids=[])          # omitted entirely
    with pytest.raises(ValueError, match="owner"):
        gen._passages(owner_id="", source_id=None, concept_ids=[])   # empty is not "no filter"


def test_demos_refuses_to_run_without_an_owner(world):
    gen = QuestionGenerator(world["db"], world["api"].ai)
    with pytest.raises(TypeError):
        gen._demos(["d1"])
    with pytest.raises(ValueError, match="owner"):
        gen._demos(["d1"], owner_id="")


def test_generate_refuses_to_run_without_an_owner(world):
    gen = QuestionGenerator(world["db"], world["api"].ai)
    with pytest.raises(TypeError):
        gen.generate(notebook_id=world["b_notebook"], count=1)


# ---------------------------------------------------------------------------
# The remediation tool must actually find a leak
# ---------------------------------------------------------------------------

def test_the_audit_tool_finds_rows_written_before_the_fix(tmp_path):
    """
    Seeded by direct INSERT, because the code path that produced these rows is
    now closed. A detector that has never seen a positive is not a detector.
    """
    db = Database(tmp_path / "leaked.db")
    a = db.create_user("a@leak.test", "correct-horse")
    b = db.create_user("b@leak.test", "correct-horse")
    ts = now_iso()
    db.execute("INSERT INTO notebooks (id,owner_id,title,created_at) VALUES ('nbA',?,'A',?)", (a, ts))
    db.execute("INSERT INTO notebooks (id,owner_id,title,created_at) VALUES ('nbB',?,'B',?)", (b, ts))
    for sid, nb in (("srcA", "nbA"), ("srcB", "nbB")):
        db.execute("INSERT INTO sources (id,notebook_id,kind,status,uploaded_at)"
                   " VALUES (?,?,'text','extracted',?)", (sid, nb, ts))
        db.execute("INSERT INTO source_chunks (id,source_id,ordinal,text,locator_json,status)"
                   " VALUES (?,?,1,?,'{}','processed')", ("chk" + sid, sid, "text of " + sid))
    cols = ("id,primary_notebook_id,source_id,chunk_id,stem,options_json,correct_index,"
            "rationale,generated_at,validation_status")
    for qid, nb, src in (("qLEAK", "nbB", "srcA"), ("qOK", "nbB", "srcB"), ("qA", "nbA", "srcA")):
        db.execute(f"INSERT INTO questions ({cols}) VALUES (?,?,?,?,?,?,0,'r',?,'approved')",
                   (qid, nb, src, "chk" + src, "Stem " + qid, json.dumps(["a", "b"]), ts))
    db.execute("INSERT INTO attempts (id,question_id,user_id,user_answer,correct_answer,"
               "is_correct,user_colour,concepts_tested_json,knowledge_gaps_json,"
               "source_refs_json,created_at) VALUES ('attX','qA',?,0,0,1,'GREEN','[]','[]','[]',?)",
               (b, ts))
    db.close()

    out = subprocess.run(
        [sys.executable, str(ROOT / "tools_cross_user_audit.py"),
         str(tmp_path / "leaked.db"), "--json"],
        capture_output=True, text=True, check=False)
    report = json.loads(out.stdout)

    assert [r["question_id"] for r in report["leaked_questions"]] == ["qLEAK"], \
        "the detector must find the cross-user question and only that one"
    assert [r["attempt_id"] for r in report["leaked_attempts"]] == ["attX"]
    assert report["exposure_pairs"][0]["questions"] == 1
    assert out.returncode == 1, "a finding must be a non-zero exit for CI"


def test_the_audit_tool_is_quiet_on_a_clean_database(world, tmp_path):
    out = subprocess.run(
        [sys.executable, str(ROOT / "tools_cross_user_audit.py"),
         str(world["db"].path), "--json"],
        capture_output=True, text=True, check=False)
    report = json.loads(out.stdout)
    assert report["leaked_questions"] == [] and report["leaked_attempts"] == []
    assert out.returncode == 0
