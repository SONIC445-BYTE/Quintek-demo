"""
Phase 2: ingestion. Chunking, locators, resumability, and honest failure.
"""

from __future__ import annotations

import json

import pytest

from student.api import StudentAPI
from student.db import Database, now_iso
from student.ingestion import (ExtractedPage, ExtractionUnavailable, IngestionEngine,
                               chunk_pages, extract_for_kind, extract_plain_text)


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "q.db")


@pytest.fixture
def engine(db, tmp_path):
    eng = IngestionEngine(db, storage_dir=tmp_path / "src")
    yield eng
    eng.stop()


@pytest.fixture
def setup(db, engine):
    api = StudentAPI(db, engine=engine)
    uid = db.create_user("l@example.com", "correct-horse")
    token = db.issue_token(uid)
    _, nb = api.handle("POST", "/notebooks", {}, {"title": "Renal"}, token)
    return api, token, nb["id"], uid


# ---------------------------------------------------------------------------
# Chunking and locators
# ---------------------------------------------------------------------------

def test_paragraphs_become_locatable_units():
    pages = extract_plain_text("First para.\n\nSecond para.\n\nThird para.")
    assert [p.locator["paragraph"] for p in pages] == [1, 2, 3]


def test_short_pages_are_combined_into_one_chunk():
    pages = [ExtractedPage(i, f"Sentence {i}. " * 5, {"page": i}) for i in range(1, 4)]
    chunks = chunk_pages(pages)
    assert len(chunks) == 1
    text, loc = chunks[0].text, chunks[0].locator
    assert "Sentence 1" in text and "Sentence 3" in text
    # The locator records both ends, so provenance survives the merge.
    assert loc["page"] == 1
    assert loc["spans_to"] == {"page": 3}


def test_an_oversized_page_is_split_on_sentence_boundaries():
    long_page = ExtractedPage(1, "This is a sentence about ferritin. " * 200, {"page": 7})
    chunks = chunk_pages([long_page])
    assert len(chunks) > 1
    for chunk in chunks:
        text, loc = chunk.text, chunk.locator
        assert loc["page"] == 7, "every part must still point at the page it came from"
        assert "part" in loc
        # Split on sentence boundaries, so no chunk ends mid-sentence.
        assert text.rstrip().endswith("."), f"chunk ends mid-sentence: {text[-40:]!r}"


def test_a_trailing_scrap_is_merged_backwards_not_shipped_alone():
    """A 40-character chunk carries no usable context for extraction."""
    pages = [ExtractedPage(1, "A. " * 700, {"page": 1}), ExtractedPage(2, "Tiny.", {"page": 2})]
    chunks = chunk_pages(pages)
    assert all(len(c.text) >= 100 for c in chunks), [len(c.text) for c in chunks]


def test_every_chunk_carries_a_locator():
    pages = [ExtractedPage(i, f"Body text for page {i}. " * 30, {"page": i}) for i in range(1, 6)]
    for chunk in chunk_pages(pages):
        loc = chunk.locator
        assert loc, "a chunk with no locator cannot answer 'where did this come from'"
        assert "page" in loc


# ---------------------------------------------------------------------------
# Extraction: what is supported, and honest refusal for what is not
# ---------------------------------------------------------------------------

def test_unsupported_kinds_say_why_rather_than_returning_nothing():
    """Empty text would mark the source 'extracted' with no content, which
    reads as a document that legitimately had none."""
    for kind, expected in [("image", "OCR"), ("video", "transcript"), ("link", "fetching")]:
        with pytest.raises(ExtractionUnavailable, match=expected):
            extract_for_kind(kind, raw_text="", url="http://example.com")


def test_empty_text_source_is_refused():
    with pytest.raises(ExtractionUnavailable):
        extract_for_kind("text", raw_text="   ")


# ---------------------------------------------------------------------------
# End-to-end through the API
# ---------------------------------------------------------------------------

def test_text_source_ingests_end_to_end(setup, db, engine):
    api, token, nb_id, _ = setup
    body = {"kind": "text", "text": "\n\n".join(
        f"Paragraph {i} discusses ferritin and iron studies in detail. " * 6
        for i in range(1, 6))}
    status, res = api.handle("POST", f"/notebooks/{nb_id}/sources", {}, body, token)
    assert status == 202
    assert engine.wait_idle(30)

    _, progress = api.handle("GET", f"/sources/{res['source_id']}/progress", {}, {}, token)
    assert progress["status"] == "extracted"
    assert progress["chunks_total"] >= 1
    assert progress["chunks_processed"] == progress["chunks_total"]
    assert progress["percent"] == 100.0

    stored = db.query("SELECT * FROM source_chunks WHERE source_id = ?", (res["source_id"],))
    assert stored, "chunks must be persisted, not held in memory"
    for chunk in stored:
        assert json.loads(chunk["locator_json"]), "locator lost on the way to the database"


def test_a_failed_source_records_why(setup, engine):
    api, token, nb_id, _ = setup
    status, res = api.handle("POST", f"/notebooks/{nb_id}/sources", {},
                             {"kind": "video", "filename": "lecture.mp4"}, token)
    assert status == 202
    assert engine.wait_idle(30)
    _, progress = api.handle("GET", f"/sources/{res['source_id']}/progress", {}, {}, token)
    assert progress["status"] == "failed"
    assert "transcript" in progress["error"]


def test_progress_percent_is_null_before_it_is_measurable(setup, db):
    """0% would read as 'no progress'; null says 'not yet known'."""
    api, token, nb_id, uid = setup
    db.execute("INSERT INTO sources (id,notebook_id,kind,status,uploaded_at)"
               " VALUES ('s-new',?,'text','uploaded',?)", (nb_id, now_iso()))
    _, progress = api.handle("GET", "/sources/s-new/progress", {}, {}, token)
    assert progress["percent"] is None


# ---------------------------------------------------------------------------
# Resumability -- the property that makes a 300-page source affordable
# ---------------------------------------------------------------------------

class _FlakyExtractor:
    """Fails on a nominated chunk ordinal, succeeds otherwise."""

    def __init__(self, fail_on_ordinal: int):
        self.fail_on = fail_on_ordinal
        self.seen: list[str] = []

    def extract_for_chunk(self, *, source_id, chunk_id, text, locator):
        self.seen.append(chunk_id)
        if locator.get("_ordinal") == self.fail_on:
            raise RuntimeError("simulated extractor failure")


def test_one_failed_chunk_does_not_fail_the_others(db, tmp_path):
    engine = IngestionEngine(db, storage_dir=tmp_path / "s")
    try:
        uid = db.create_user("r@example.com", "correct-horse")
        db.execute("INSERT INTO notebooks (id,owner_id,title,created_at) VALUES ('nb',?,'N',?)",
                   (uid, now_iso()))
        db.execute("INSERT INTO sources (id,notebook_id,kind,status,uploaded_at)"
                   " VALUES ('s','nb','text','uploaded',?)", (now_iso(),))

        class FailSecond:
            def extract_for_chunk(self, *, source_id, chunk_id, text, locator):
                if locator.get("part") == 2 or "SECOND" in text:
                    raise RuntimeError("boom")

        engine.concept_extractor = FailSecond()
        engine.process_source("s", raw_text="\n\n".join(
            ["A" * 900, "SECOND " * 200, "C" * 900]))

        chunks = db.query("SELECT status FROM source_chunks WHERE source_id='s' ORDER BY ordinal")
        statuses = [c["status"] for c in chunks]
        assert "failed" in statuses, "the bad chunk must be recorded as failed"
        assert "processed" in statuses, "a bad chunk must not take the good ones with it"
    finally:
        engine.stop()


def test_reprocessing_resumes_and_does_not_redo_finished_chunks(db, tmp_path):
    engine = IngestionEngine(db, storage_dir=tmp_path / "s")
    try:
        uid = db.create_user("r@example.com", "correct-horse")
        db.execute("INSERT INTO notebooks (id,owner_id,title,created_at) VALUES ('nb',?,'N',?)",
                   (uid, now_iso()))
        db.execute("INSERT INTO sources (id,notebook_id,kind,status,uploaded_at)"
                   " VALUES ('s','nb','text','uploaded',?)", (now_iso(),))

        seen: list[str] = []

        class Counting:
            def extract_for_chunk(self, *, source_id, chunk_id, text, locator):
                seen.append(chunk_id)

        engine.concept_extractor = Counting()
        engine.process_source("s", raw_text="\n\n".join(["X" * 900 for _ in range(4)]))
        first_pass = len(seen)
        assert first_pass > 0

        # Mark one chunk failed, then re-run: only that chunk should be retried.
        db.execute("UPDATE source_chunks SET status='failed' WHERE source_id='s'"
                   " AND ordinal = (SELECT MIN(ordinal) FROM source_chunks WHERE source_id='s')")
        seen.clear()
        engine._process_chunks("s")
        assert len(seen) == 1, f"resume reprocessed {len(seen)} chunks instead of the 1 that failed"
    finally:
        engine.stop()


def test_reingesting_replaces_chunks_rather_than_duplicating_them(db, tmp_path):
    engine = IngestionEngine(db, storage_dir=tmp_path / "s")
    try:
        uid = db.create_user("r@example.com", "correct-horse")
        db.execute("INSERT INTO notebooks (id,owner_id,title,created_at) VALUES ('nb',?,'N',?)",
                   (uid, now_iso()))
        db.execute("INSERT INTO sources (id,notebook_id,kind,status,uploaded_at)"
                   " VALUES ('s','nb','text','uploaded',?)", (now_iso(),))
        text = "\n\n".join(["Y" * 900 for _ in range(3)])
        engine.process_source("s", raw_text=text)
        first = db.query_one("SELECT COUNT(*) c FROM source_chunks WHERE source_id='s'")["c"]
        engine.process_source("s", raw_text=text)
        second = db.query_one("SELECT COUNT(*) c FROM source_chunks WHERE source_id='s'")["c"]
        assert first == second
    finally:
        engine.stop()


# ---------------------------------------------------------------------------
# Failure reporting
#
# Found by running the full lifecycle against a deliberately broken extractor:
# every chunk failed, and the source still reported status='extracted',
# error=null, 0% processed. Diagnosing it needed a direct query against
# source_chunks. The reasons were being stored and then thrown away.
# ---------------------------------------------------------------------------

class _AlwaysFailsExtractor:
    def extract_for_chunk(self, **kwargs):
        raise RuntimeError("the model refused")


class _FailsOnceExtractor:
    """Fails the first chunk, then succeeds -- the partial-success case."""

    def __init__(self):
        self.calls = 0

    def extract_for_chunk(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("the model refused")
        return {}


def _seed_source(db, text):
    from student.db import new_id, now_iso

    uid = db.create_user("ing@example.test", "a-long-enough-password")
    nb, sid, stamp = new_id("nb"), new_id("src"), now_iso()
    db.execute("INSERT INTO notebooks (id, owner_id, title, created_at) VALUES (?,?,?,?)",
               (nb, uid, "Renal", stamp))
    db.execute("INSERT INTO sources (id, notebook_id, kind, status, uploaded_at)"
               " VALUES (?,?,?,?,?)", (sid, nb, "text", "uploaded", stamp))
    return uid, nb, sid


LONG_TEXT = ("Nephrotic syndrome is defined by heavy proteinuria exceeding 3.5 grams per day, "
             "hypoalbuminaemia, oedema and hyperlipidaemia. Minimal change disease is the "
             "commonest cause in children and responds to corticosteroids. " * 14)


def test_a_source_whose_every_chunk_failed_is_not_reported_as_extracted(tmp_path):
    from student.db import Database
    from student.ingestion import IngestionEngine

    db = Database(tmp_path / "s.db")
    _, _, sid = _seed_source(db, LONG_TEXT)
    engine = IngestionEngine(db, concept_extractor=_AlwaysFailsExtractor())
    engine.enqueue_source(sid, raw_text=LONG_TEXT)
    engine.wait_idle(timeout=30)

    row = db.query_one("SELECT status, error FROM sources WHERE id = ?", (sid,))
    assert row["status"] == "failed"
    # And the reason names the underlying error, not just a count.
    assert "chunk(s) failed" in row["error"]
    assert "the model refused" in row["error"]


def test_a_partly_failed_source_still_succeeds_but_says_so(tmp_path):
    from student.db import Database
    from student.ingestion import IngestionEngine

    db = Database(tmp_path / "s.db")
    _, _, sid = _seed_source(db, LONG_TEXT)
    engine = IngestionEngine(db, concept_extractor=_FailsOnceExtractor())
    engine.enqueue_source(sid, raw_text=LONG_TEXT)
    engine.wait_idle(timeout=30)

    row = db.query_one("SELECT status, error FROM sources WHERE id = ?", (sid,))
    # One bad page must not cost the book...
    assert row["status"] == "extracted"
    # ...but it must not be silent either.
    assert "failed concept extraction" in row["error"]


def test_a_fully_successful_source_carries_no_error(tmp_path):
    from student.db import Database
    from student.ingestion import IngestionEngine

    db = Database(tmp_path / "s.db")
    _, _, sid = _seed_source(db, LONG_TEXT)

    class _Works:
        def extract_for_chunk(self, **kwargs):
            return {}

    engine = IngestionEngine(db, concept_extractor=_Works())
    engine.enqueue_source(sid, raw_text=LONG_TEXT)
    engine.wait_idle(timeout=30)

    row = db.query_one("SELECT status, error FROM sources WHERE id = ?", (sid,))
    assert row["status"] == "extracted"
    assert row["error"] is None


def test_progress_explains_chunk_failures_rather_than_only_counting_them(tmp_path):
    from student.api import StudentAPI
    from student.db import Database
    from student.ingestion import IngestionEngine

    db = Database(tmp_path / "s.db")
    uid, _, sid = _seed_source(db, LONG_TEXT)
    engine = IngestionEngine(db, concept_extractor=_AlwaysFailsExtractor())
    engine.enqueue_source(sid, raw_text=LONG_TEXT)
    engine.wait_idle(timeout=30)

    progress = StudentAPI(db, engine=engine).source_progress(uid, sid)
    assert progress["chunks_failed"] > 0
    assert progress["chunk_errors"], "a failed chunk must carry its reason"
    assert "the model refused" in progress["chunk_errors"][0]["error"]
    assert progress["error"], "the source-level error must not be null when every chunk failed"


# --------------------------------------------------------------------------
# Confidence, and the gate that acts on it
# --------------------------------------------------------------------------

from student.ingestion import (METHOD_MIXED, METHOD_PDF_TEXT_LAYER,  # noqa: E402
                               METHOD_PLAIN_TEXT, QUALITY_DEGRADED, QUALITY_OK,
                               QUALITY_REJECTED, Chunk, assess,
                               quality_thresholds)


def test_the_two_working_paths_state_their_confidence_rather_than_defaulting():
    """
    1.0 here is a claim about these paths, not an optimistic default: pasted
    text has no extraction step and a PDF text layer is the document's own
    characters. An OCR path must supply its own number.
    """
    page = extract_plain_text("some notes")[0]
    assert page.confidence == 1.0
    assert page.extraction_method == METHOD_PLAIN_TEXT
    assert page.needs_review is False


def test_confidence_survives_chunking():
    pages = [ExtractedPage(1, "Body. " * 40, {"page": 1},
                           confidence=0.4, extraction_method="ocr_print",
                           needs_review=True)]
    chunk = chunk_pages(pages)[0]
    assert chunk.confidence == 0.4
    assert chunk.extraction_method == "ocr_print"
    assert chunk.needs_review is True


def test_a_merged_chunk_inherits_the_WORST_confidence_not_the_average():
    """
    A chunk containing one badly-read page is as untrustworthy as that page.
    Averaging 0.2 against four clean pages would hide exactly the thing the
    gate exists to catch.
    """
    pages = [ExtractedPage(1, "Short one. ", {"page": 1}, confidence=1.0,
                           extraction_method=METHOD_PDF_TEXT_LAYER),
             ExtractedPage(2, "Short two. ", {"page": 2}, confidence=0.2,
                           extraction_method="ocr_print", needs_review=True)]
    chunk = chunk_pages(pages)[0]
    assert chunk.confidence == 0.2, "min, not mean"
    assert chunk.needs_review is True, "needs_review is OR across the merge"
    assert chunk.extraction_method == METHOD_MIXED, \
        "a span read two ways cannot honestly claim one method"


def test_a_source_with_no_text_is_rejected():
    verdict = assess([])
    assert verdict.quality == QUALITY_REJECTED
    assert verdict.rejected is True


def test_a_span_with_no_anchor_is_rejected():
    """The UI promises a concept keeps its page reference. Enforced, not intended."""
    verdict = assess([Chunk("text", {})])
    assert verdict.quality == QUALITY_REJECTED
    assert any("anchor" in r for r in verdict.reasons)


def test_low_mean_confidence_is_rejected_and_says_why():
    t = quality_thresholds()
    verdict = assess([Chunk("a", {"page": 1}, confidence=0.1)])
    assert verdict.quality == QUALITY_REJECTED
    assert any("not read reliably enough" in r for r in verdict.reasons)
    assert verdict.mean_confidence < t["reject_below_mean_confidence"]


def test_a_minority_of_low_confidence_spans_is_degraded_not_rejected():
    """Degraded proceeds -- with the flag recorded -- rather than being lost."""
    chunks = ([Chunk(f"low{i}", {"page": i}, confidence=0.6) for i in range(4)]
              + [Chunk(f"ok{i}", {"page": 10 + i}, confidence=1.0) for i in range(6)])
    verdict = assess(chunks)
    assert verdict.quality == QUALITY_DEGRADED
    assert verdict.rejected is False
    assert verdict.low_confidence_ratio == 0.4


def test_todays_material_passes_the_gate_unchanged():
    """Both shipped paths are exact, so the gate must be invisible to them."""
    verdict = assess(chunk_pages(extract_plain_text("alpha beta\n\ngamma delta")))
    assert verdict.quality == QUALITY_OK
    assert verdict.mean_confidence == 1.0
    assert verdict.reasons == []


def test_a_missing_config_does_not_loosen_the_gate(tmp_path):
    """A deployment that lost its config must not get a more permissive gate."""
    absent = quality_thresholds(tmp_path / "nope.json")
    assert absent == quality_thresholds()


def test_a_rejected_source_never_reaches_concept_extraction(tmp_path):
    """
    The whole point of the gate. If this passes concepts through, OCR that
    misread a dose becomes a confidently-worded question and every stage
    reports success -- the error surfaces only in the learner's memory.
    """
    from student.db import Database
    from student.ingestion import IngestionEngine

    db = Database(tmp_path / "s.db")
    db.initialise()

    extracted = []

    class _Recorder:
        def extract_for_chunk(self, *a, **kw):
            extracted.append(a)
            return []

    engine = IngestionEngine(db, concept_extractor=_Recorder(),
                            storage_dir=tmp_path / "src")
    try:
        # An extractor that returns text nobody could read reliably.
        def _unreadable(kind, *, path=None, raw_text="", url=""):
            return [ExtractedPage(1, "smudged illegible text", {"page": 1},
                                  confidence=0.05, extraction_method="ocr_print",
                                  needs_review=True)]

        engine_module = __import__("student.ingestion", fromlist=["x"])
        original = engine_module.extract_for_kind
        engine_module.extract_for_kind = _unreadable
        try:
            db.execute(
                "INSERT INTO users (id, email, password_salt, password_hash, created_at)"
                " VALUES ('u1','x@example.com','s','h','2026-01-01T00:00:00Z')")
            db.execute(
                "INSERT INTO notebooks (id, owner_id, title, subject, created_at)"
                " VALUES ('nb1','u1','T','S','2026-01-01T00:00:00Z')")
            db.execute(
                "INSERT INTO sources (id, notebook_id, kind, filename, storage_key,"
                " mime_type, byte_size, status, uploaded_at)"
                " VALUES ('src1','nb1','image','scan.jpg','','',0,'uploaded',"
                "'2026-01-01T00:00:00Z')")
            engine.process_source("src1")
        finally:
            engine_module.extract_for_kind = original

        row = db.query_one("SELECT status, quality, error FROM sources WHERE id='src1'")
        assert row["quality"] == QUALITY_REJECTED
        assert row["status"] == "failed"
        assert extracted == [], "concept extraction ran on rejected text"
        # The learner is told why, in the source's own error field.
        assert "not read reliably enough" in (row["error"] or "")
    finally:
        engine.stop()
