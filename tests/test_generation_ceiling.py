"""
The spend ceiling on question generation: 50 model calls per account per
rolling 24 hours by default, configurable, charged through the existing
`operations.spend_guard` / `charge` machinery.

50 is a starting value for the testing phase, not a researched limit.

What must hold:
  * the unit is one GENERATION MODEL CALL -- one per generate request,
    whatever the question count -- charged immediately before the call;
  * the call past the ceiling never reaches the model, is not written as
    spend, and answers 429 with a sentence a learner can act on;
  * a request refused before the model (nothing to ground it) costs nothing;
  * a call that reached the model counts even if its reply was unusable;
  * per account, and reloaded from `spend_log`, not held in memory;
  * the configuration refuses a nonsense value rather than defaulting.
"""

from __future__ import annotations

import json

import pytest

from benchmark.providers.base import GenerationResponse
from student import operations as ops
from student.ai import AIEngine
from student.api import StudentAPI
from student.db import new_id, now_iso
from student.generation import (DEFAULT_GENERATION_CALLS_PER_DAY,
                                GENERATION_CALLS_PER_DAY_ENV, QuestionGenerator,
                                generation_calls_per_day)

ITEM = {"stem": "Low FeNa means?", "options": ["Pre-renal", "Intrinsic"], "correct_index": 0,
        "passage": 1, "rationale": "r", "concepts_tested": []}


class Provider:
    name, model, model_version = "scripted", "scripted/model", "1.0"

    def __init__(self, reply):
        self.reply, self.calls = reply, 0

    def generate(self, req):
        self.calls += 1
        return GenerationResponse(item_id=req.item_id, raw_output=json.dumps(self.reply),
                                  parsed=None, provider="scripted", model="scripted/model",
                                  model_version="1.0", latency_ms=5.0, input_tokens=10,
                                  output_tokens=20)


@pytest.fixture
def world(any_backend, monkeypatch):
    monkeypatch.delenv(GENERATION_CALLS_PER_DAY_ENV, raising=False)
    db = any_backend.student()
    provider = Provider({"questions": [ITEM, ITEM, ITEM]})
    ai = AIEngine(db, provider_factory=lambda c: provider, development_candidate="cand-dev")

    def build(limit=3):
        return StudentAPI(db, ai=ai, generator=QuestionGenerator(db, ai, calls_per_day=limit))

    def learner(api, email, *, with_source=True):
        tok = api.handle("POST", "/auth/register", {},
                         {"email": email, "password": "correct-horse"}, None)[1]["token"]
        nid = api.handle("POST", "/notebooks", {}, {"title": "Renal", "subject": "renal"},
                         tok)[1]["id"]
        if with_source:
            sid = new_id("src")
            db.execute("INSERT INTO sources (id,notebook_id,kind,status,uploaded_at)"
                       " VALUES (?,?,?,?,?)", (sid, nid, "text", "extracted", now_iso()))
            db.execute("INSERT INTO source_chunks (id,source_id,ordinal,text,locator_json,"
                       "status) VALUES (?,?,?,?,?,?)",
                       (new_id("ch"), sid, 1, "FeNa below 1% suggests pre-renal injury.",
                        json.dumps({"page": 1}), "processed"))
        return tok, nid

    w = type("W", (), {})()
    w.db, w.provider, w.build, w.learner = db, provider, build, learner
    return w


def generate(api, tok, nid, count=3):
    return api.handle("POST", f"/notebooks/{nid}/questions", {},
                      {"count": count, "validate": False}, tok)


def spent(db, uid):
    return db.query_one("SELECT COUNT(*) n FROM spend_log WHERE user_id = ? AND operation = ?",
                        (uid, ops.GENERATION))["n"]


def uid_of(db, email):
    return db.query_one("SELECT id FROM users WHERE email = ?", (email,))["id"]


def test_the_default_is_fifty_and_the_generator_uses_it(world, monkeypatch):
    assert DEFAULT_GENERATION_CALLS_PER_DAY == 50
    assert generation_calls_per_day() == 50
    assert QuestionGenerator(world.db, None).calls_per_day == 50
    monkeypatch.setenv(GENERATION_CALLS_PER_DAY_ENV, "7")
    assert QuestionGenerator(world.db, None).calls_per_day == 7


@pytest.mark.parametrize("raw", ["0", "-5", "fifty", "2.5"])
def test_a_nonsense_ceiling_is_refused_not_defaulted(monkeypatch, raw):
    monkeypatch.setenv(GENERATION_CALLS_PER_DAY_ENV, raw)
    with pytest.raises(ValueError, match="positive integer"):
        generation_calls_per_day()


def test_one_request_is_one_call_whatever_the_count(world):
    api = world.build(limit=3)
    tok, nid = world.learner(api, "a@example.com")
    st, out = generate(api, tok, nid, count=3)
    assert st == 201 and out["count"] == 3, out
    assert world.provider.calls == 1
    assert spent(world.db, uid_of(world.db, "a@example.com")) == 1


def test_the_call_past_the_ceiling_never_reaches_the_model(world):
    api = world.build(limit=3)
    tok, nid = world.learner(api, "a@example.com")
    for _ in range(3):
        assert generate(api, tok, nid)[0] == 201
    before = world.provider.calls
    st, out = generate(api, tok, nid)
    assert st == 429, out
    assert "3 question-generation requests in the last 24 hours" in out["error"]
    assert out["limit"] == 3
    assert world.provider.calls == before, "the refused request still called the model"
    assert spent(world.db, uid_of(world.db, "a@example.com")) == 3, (
        "the refused call was written to the spend log")
    assert "usr_" not in out["error"], "the account id leaked into the message"


def test_a_request_refused_before_the_model_costs_nothing(world):
    api = world.build(limit=3)
    tok, nid = world.learner(api, "a@example.com", with_source=False)
    st, out = generate(api, tok, nid)
    assert st == 422, out                 # nothing to ground it in
    assert world.provider.calls == 0
    assert spent(world.db, uid_of(world.db, "a@example.com")) == 0


def test_a_call_whose_reply_was_unusable_still_counts(world):
    """The provider billed for it. Not counting it would let a client that
    triggers bad replies spend without limit."""
    api = world.build(limit=3)
    tok, nid = world.learner(api, "a@example.com")
    world.provider.reply = {"not questions": True}
    st, _ = generate(api, tok, nid)
    assert st == 422
    assert world.provider.calls == 1
    assert spent(world.db, uid_of(world.db, "a@example.com")) == 1


def test_the_ceiling_is_per_account(world):
    api = world.build(limit=1)
    a_tok, a_nid = world.learner(api, "a@example.com")
    b_tok, b_nid = world.learner(api, "b@example.com")
    assert generate(api, a_tok, a_nid)[0] == 201
    assert generate(api, a_tok, a_nid)[0] == 429
    assert generate(api, b_tok, b_nid)[0] == 201, "A's spend used B's allowance"


def test_the_ceiling_survives_a_restart(world):
    """A new server process -- a new StudentAPI and generator -- must read the
    account's spend back from spend_log, not start from zero."""
    tok, nid = world.learner(world.build(limit=2), "a@example.com")
    first = world.build(limit=2)
    assert generate(first, tok, nid)[0] == 201
    assert generate(first, tok, nid)[0] == 201
    restarted = world.build(limit=2)
    assert generate(restarted, tok, nid)[0] == 429


def test_spend_older_than_24_hours_frees_up(world):
    api = world.build(limit=1)
    tok, nid = world.learner(api, "a@example.com")
    uid = uid_of(world.db, "a@example.com")
    world.db.execute("INSERT INTO spend_log (id,user_id,operation,units,note,created_at)"
                     " VALUES (?,?,?,?,?,?)", (new_id("spn"), uid, ops.GENERATION, 1,
                                               "yesterday", "2000-01-01T00:00:00Z"))
    assert generate(api, tok, nid)[0] == 201, "a call from long ago still counted"
