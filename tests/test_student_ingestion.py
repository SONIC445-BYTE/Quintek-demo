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
    text, loc = chunks[0]
    assert "Sentence 1" in text and "Sentence 3" in text
    # The locator records both ends, so provenance survives the merge.
    assert loc["page"] == 1
    assert loc["spans_to"] == {"page": 3}


def test_an_oversized_page_is_split_on_sentence_boundaries():
    long_page = ExtractedPage(1, "This is a sentence about ferritin. " * 200, {"page": 7})
    chunks = chunk_pages([long_page])
    assert len(chunks) > 1
    for text, loc in chunks:
        assert loc["page"] == 7, "every part must still point at the page it came from"
        assert "part" in loc
        # Split on sentence boundaries, so no chunk ends mid-sentence.
        assert text.rstrip().endswith("."), f"chunk ends mid-sentence: {text[-40:]!r}"


def test_a_trailing_scrap_is_merged_backwards_not_shipped_alone():
    """A 40-character chunk carries no usable context for extraction."""
    pages = [ExtractedPage(1, "A. " * 700, {"page": 1}), ExtractedPage(2, "Tiny.", {"page": 2})]
    chunks = chunk_pages(pages)
    assert all(len(t) >= 100 for t, _ in chunks), [len(t) for t, _ in chunks]


def test_every_chunk_carries_a_locator():
    pages = [ExtractedPage(i, f"Body text for page {i}. " * 30, {"page": i}) for i in range(1, 6)]
    for _, loc in chunk_pages(pages):
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
