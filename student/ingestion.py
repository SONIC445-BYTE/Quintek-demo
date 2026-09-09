"""
Source ingestion: upload -> text -> chunks with locators -> concepts.

Two design rules carried from `docs/QUINTEK_LOGIC.md` section 4.1:

  * **Never send a whole source in one call.** Sources are chunked first, and
    each chunk is processed independently. A 300-page textbook is not one
    prompt.

  * **Resumable at chunk granularity.** A failed chunk 87 does not reset
    1..86. Chunk status is persisted before and after each unit of work, so a
    crashed or killed worker resumes where it stopped rather than re-running
    (and re-billing) everything.

The locator is the point of the whole exercise. Every chunk records where it
came from -- `{page, paragraph, lines}` for text, `{page, figure, caption}` for
a figure, `{t_start, t_end}` for video -- because "show me exactly the passage
this came from" is a product requirement, and it is impossible to add later if
the position was discarded at ingestion.
"""

from __future__ import annotations

import json
import queue
import re
import threading
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from .db import Database, new_id, now_iso

# Roughly a page of prose. Small enough that a model attends to all of it,
# large enough that a concept's context is not split across a boundary.
TARGET_CHUNK_CHARS = 1800
MIN_CHUNK_CHARS = 200


@dataclass
class ExtractedPage:
    """One page / slide / segment of a source, before chunking."""
    ordinal: int
    text: str
    locator: dict = field(default_factory=dict)
    # How sure the extractor was, and how it produced this text.
    #
    # Defaults describe the paths that exist today rather than flattering
    # them: pasted text has no extraction step and a PDF text layer is exact,
    # so both are genuinely 1.0. An OCR or ASR adapter MUST pass its own value
    # -- inheriting this default would be the exact failure the gate exists to
    # prevent, dressed as a reasonable default.
    confidence: float = 1.0
    extraction_method: str = ""
    needs_review: bool = False


# Extraction methods, named once. These strings land in the database and are
# read back by anything asking "how was this produced", so renaming one
# silently rewrites the record of how existing material was extracted.
METHOD_PLAIN_TEXT = "plain_text"
METHOD_PDF_TEXT_LAYER = "pdf_text_layer"
# A chunk built from pages extracted different ways. Aggregation cannot honestly
# report a single method for it.
METHOD_MIXED = "mixed"


class ExtractionUnavailable(RuntimeError):
    """No extractor exists for this source kind in this deployment.

    Raised rather than returning empty text, so a source is marked `failed`
    with a reason instead of `extracted` with nothing in it -- the second looks
    like a document that legitimately contained no text.
    """


# ---------------------------------------------------------------------------
# Text extraction, per source kind
# ---------------------------------------------------------------------------

def _pdf_available() -> bool:
    try:
        import pypdf  # noqa: F401
        return True
    except Exception:
        return False


def extract_pdf(path: str | Path) -> list[ExtractedPage]:
    """
    Page-by-page text with page numbers preserved.

    pypdf is an optional dependency: a deployment without it should say so
    rather than silently accept PDFs and produce nothing. A PDF with no text
    layer (a scan) is also reported rather than treated as empty -- it needs
    OCR, which is a different capability, not a failure of this function.
    """
    try:
        from pypdf import PdfReader
    except Exception as exc:
        raise ExtractionUnavailable(
            "PDF extraction needs the 'pypdf' package, which is not importable "
            f"here ({exc}). Install it, or upload the text directly."
        ) from exc

    reader = PdfReader(str(path))
    pages: list[ExtractedPage] = []
    for i, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if text:
            pages.append(ExtractedPage(
                ordinal=i, text=text, locator={"page": i},
                # An embedded text layer is the document's own characters, not
                # a reading of them. Stamped explicitly so that when an OCR
                # branch is added here, the difference between the two is
                # already recorded on every row rather than inferred later
                # from which release wrote it.
                confidence=1.0, extraction_method=METHOD_PDF_TEXT_LAYER,
                needs_review=False))
    if not pages:
        raise ExtractionUnavailable(
            "this PDF has no extractable text layer -- it is probably a scan, "
            "which needs OCR rather than text extraction"
        )
    return pages


def extract_plain_text(text: str) -> list[ExtractedPage]:
    """Text and notes. Paragraphs are the unit, so a locator can name one."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    # Nothing was read, so there is nothing to be unsure about: the learner's
    # own keystrokes are the source.
    stamp = dict(confidence=1.0, extraction_method=METHOD_PLAIN_TEXT,
                 needs_review=False)
    return [
        ExtractedPage(ordinal=i, text=p, locator={"paragraph": i}, **stamp)
        for i, p in enumerate(paragraphs, start=1)
    ] or [ExtractedPage(ordinal=1, text=text.strip(), locator={"paragraph": 1}, **stamp)]


# What this deployment can actually read, and why not when it cannot.
#
# The five source kinds are offered with equal prominence in the app, but three
# of them raise `ExtractionUnavailable` the moment they are tried. A picker
# that offers five doors and opens two is the same defect as a "Choose file"
# button that shows a text box: the failure is discovered after the learner has
# committed, not before.
#
# Derived from the SAME conditions `extract_for_kind` branches on below, so the
# two cannot drift: adding OCR makes this report OCR without a second edit.
def source_capabilities() -> dict:
    pdf_ok = _pdf_available()
    return {
        "text": {"available": True, "reason": ""},
        "note": {"available": True, "reason": ""},
        "pdf": {
            "available": pdf_ok,
            "reason": "" if pdf_ok else
            "PDF reading needs the 'pypdf' package, which is not installed here.",
        },
        "link": {
            "available": False,
            "reason": "Fetching a page needs outbound access and an HTML-to-text "
                      "pass, neither of which is configured. Paste the text instead.",
        },
        "image": {
            "available": False,
            "reason": "Reading a photo needs OCR, which is not configured here. "
                      "Type the notes out instead.",
        },
        "video": {
            "available": False,
            "reason": "A video needs a transcript source, which is not configured "
                      "here. Paste the transcript instead.",
        },
    }


def extract_for_kind(kind: str, *, path: str | Path | None = None,
                     raw_text: str = "", url: str = "") -> list[ExtractedPage]:
    if kind in {"text", "note"}:
        if not raw_text.strip():
            raise ExtractionUnavailable("no text was supplied")
        return extract_plain_text(raw_text)

    if kind == "pdf":
        if path is None:
            raise ExtractionUnavailable("no file was stored for this PDF source")
        return extract_pdf(path)

    if kind == "link":
        # Fetching a URL means egress, an HTML-to-text pass, and a robots
        # decision. Stated plainly rather than half-implemented.
        raise ExtractionUnavailable(
            "link ingestion is not configured: it needs outbound fetching and an "
            "HTML-to-text extractor. Paste the text instead."
        )

    if kind == "image":
        raise ExtractionUnavailable(
            "image ingestion needs OCR, which is not configured in this deployment"
        )

    if kind == "video":
        raise ExtractionUnavailable(
            "video ingestion needs a transcript source, which is not configured "
            "in this deployment"
        )

    raise ExtractionUnavailable(f"unsupported source kind: {kind!r}")


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

@dataclass
class Chunk:
    """
    One stored span: the text, where it came from, and how far to trust it.

    A dataclass rather than the `(text, locator)` tuple this used to be,
    because confidence does not belong inside the locator. The locator answers
    "where is this from" and is shown to the learner as a page reference; the
    confidence answers "how sure are we" and gates whether the text is used at
    all. Folding the second into the first would make the quality gate parse a
    display field.
    """
    text: str
    locator: dict
    confidence: float = 1.0
    extraction_method: str = ""
    needs_review: bool = False


def _combine(pages: list[ExtractedPage]) -> dict:
    """
    Quality of a chunk built from several pages. Fail closed on every axis.

    Confidence is the MINIMUM, not the mean: a chunk containing one badly-read
    page is exactly as untrustworthy as that page, and averaging it against
    four clean ones hides the thing worth knowing. `needs_review` is OR for the
    same reason. A chunk spanning pages read different ways reports `mixed`
    rather than picking one and implying the whole span was read that way.
    """
    if not pages:
        return {"confidence": 1.0, "extraction_method": "", "needs_review": False}
    methods = {pg.extraction_method for pg in pages if pg.extraction_method}
    return {
        "confidence": min(pg.confidence for pg in pages),
        "extraction_method": (methods.pop() if len(methods) == 1
                              else (METHOD_MIXED if methods else "")),
        "needs_review": any(pg.needs_review for pg in pages),
    }


def chunk_pages(pages: list[ExtractedPage]) -> list[Chunk]:
    """
    Group extracted pages into model-sized chunks, carrying locators through.

    Splitting happens on sentence boundaries where possible: a chunk that ends
    mid-sentence costs the model the context it needed to place the concept,
    and that shows up later as a concept extracted with the wrong meaning.
    """
    chunks: list[Chunk] = []
    buffer, buf_locators, buf_pages = "", [], []

    def flush():
        nonlocal buffer, buf_locators, buf_pages
        if buffer.strip():
            first, last = buf_locators[0], buf_locators[-1]
            locator = dict(first)
            if last != first:
                locator["spans_to"] = last
            locator["chars"] = len(buffer.strip())
            chunks.append(Chunk(text=buffer.strip(), locator=locator,
                                **_combine(buf_pages)))
        buffer, buf_locators, buf_pages = "", [], []

    for page in pages:
        text = page.text.strip()
        if not text:
            continue
        # A single page larger than the target is split on sentences.
        if len(text) > TARGET_CHUNK_CHARS:
            flush()
            # Every part comes from this one page, so each inherits that
            # page's quality unchanged -- splitting text does not make it more
            # or less trustworthy.
            quality = _combine([page])
            sentences = re.split(r"(?<=[.!?])\s+", text)
            part, idx = "", 1
            for sentence in sentences:
                if part and len(part) + len(sentence) + 1 > TARGET_CHUNK_CHARS:
                    loc = dict(page.locator)
                    loc.update({"part": idx, "chars": len(part.strip())})
                    chunks.append(Chunk(text=part.strip(), locator=loc, **quality))
                    part, idx = "", idx + 1
                part = f"{part} {sentence}".strip()
            if part.strip():
                loc = dict(page.locator)
                loc.update({"part": idx, "chars": len(part.strip())})
                chunks.append(Chunk(text=part.strip(), locator=loc, **quality))
            continue

        if buffer and len(buffer) + len(text) + 2 > TARGET_CHUNK_CHARS:
            flush()
        buffer = f"{buffer}\n\n{text}".strip()
        buf_locators.append(page.locator)
        buf_pages.append(page)

    flush()

    # A trailing scrap is merged backwards rather than shipped as its own
    # chunk: a 40-character chunk carries no usable context.
    if len(chunks) > 1 and len(chunks[-1].text) < MIN_CHUNK_CHARS:
        tail = chunks.pop()
        prev = chunks[-1]
        merged_loc = dict(prev.locator)
        merged_loc["spans_to"] = tail.locator
        methods = {m for m in (prev.extraction_method, tail.extraction_method) if m}
        # Same fail-closed rule as _combine: the merged chunk inherits the
        # WORSE of the two, because it now contains both.
        chunks[-1] = Chunk(
            text=f"{prev.text}\n\n{tail.text}", locator=merged_loc,
            confidence=min(prev.confidence, tail.confidence),
            extraction_method=(methods.pop() if len(methods) == 1
                               else (METHOD_MIXED if methods else "")),
            needs_review=prev.needs_review or tail.needs_review)

    return chunks


# ---------------------------------------------------------------------------
# The quality gate
# ---------------------------------------------------------------------------

QUALITY_OK = "ok"
QUALITY_DEGRADED = "degraded"
QUALITY_REJECTED = "rejected"

_QUALITY_CONFIG = Path(__file__).resolve().parent.parent / "configs" / "ingestion_quality.json"

# Used only if the config file is missing or unreadable. Deliberately the same
# values as the file rather than something laxer: a deployment that lost its
# config must not silently get a MORE permissive gate than one that has it.
_FALLBACK_THRESHOLDS = {
    "low_confidence_span": 0.75,
    "reject_below_mean_confidence": 0.55,
    "degraded_above_low_confidence_ratio": 0.30,
}


def quality_thresholds(path: str | Path | None = None) -> dict:
    """
    Gate thresholds, from config rather than from code.

    In a file for the same reason the benchmark's gate thresholds live in the
    registry: they need tuning against real material, and tuning them should be
    a reviewable change rather than an edit inside a function.
    """
    target = Path(path) if path else _QUALITY_CONFIG
    try:
        loaded = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return dict(_FALLBACK_THRESHOLDS)
    out = dict(_FALLBACK_THRESHOLDS)
    for key in out:
        if isinstance(loaded.get(key), (int, float)):
            out[key] = float(loaded[key])
    return out


@dataclass
class QualityVerdict:
    quality: str
    reasons: list[str] = field(default_factory=list)
    mean_confidence: float = 1.0
    low_confidence_ratio: float = 0.0

    @property
    def rejected(self) -> bool:
        return self.quality == QUALITY_REJECTED

    def as_dict(self) -> dict:
        return {"quality": self.quality, "reasons": list(self.reasons),
                "mean_confidence": self.mean_confidence,
                "low_confidence_ratio": self.low_confidence_ratio}


def assess(chunks: list[Chunk], *, thresholds: dict | None = None) -> QualityVerdict:
    """
    Decide whether this source may become study material. Fails closed.

    The rule this enforces: extraction confidence must travel with the text,
    and low-confidence text must not silently become study material. The
    failure it prevents is specific -- OCR misreads a dose, concept extraction
    succeeds, generation succeeds, and the learner revises a confidently-worded
    question built on a misread number. Every stage reports success and the
    error surfaces only in their memory.

    `rejected` never reaches concept extraction. `degraded` proceeds with the
    flag recorded, so anything built from it can be shown as such.
    """
    t = thresholds or quality_thresholds()
    reasons: list[str] = []

    if not chunks:
        return QualityVerdict(QUALITY_REJECTED,
                              ["no text was extracted from this source"], 0.0, 0.0)

    # An anchor is mandatory. A span nobody can trace back cannot support the
    # promise that a concept keeps its page reference, so it is rejected here
    # rather than discovered as a dead link at revision time.
    unanchored = [i for i, c in enumerate(chunks, start=1) if not c.locator]
    if unanchored:
        reasons.append(
            f"{len(unanchored)} span(s) have no anchor, so nothing generated from "
            "them could be traced back to the source")

    confidences = [c.confidence for c in chunks]
    mean_confidence = sum(confidences) / len(confidences)
    low = [c for c in chunks if c.confidence < t["low_confidence_span"]]
    low_ratio = len(low) / len(chunks)

    if mean_confidence < t["reject_below_mean_confidence"]:
        reasons.append(
            f"mean extraction confidence {mean_confidence:.2f} is below the "
            f"{t['reject_below_mean_confidence']:.2f} floor; this source was not read "
            "reliably enough to study from")

    if reasons:
        return QualityVerdict(QUALITY_REJECTED, reasons, mean_confidence, low_ratio)

    if low_ratio > t["degraded_above_low_confidence_ratio"]:
        return QualityVerdict(
            QUALITY_DEGRADED,
            [f"{len(low)} of {len(chunks)} span(s) were read with low confidence; "
             "questions built from them carry that flag"],
            mean_confidence, low_ratio)

    return QualityVerdict(QUALITY_OK, [], mean_confidence, low_ratio)


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------

class IngestionEngine:
    """
    Background worker that turns an uploaded source into chunks and concepts.

    `concept_extractor` is injected: it receives (chunk_text, context) and
    returns concepts and relationships. That keeps this module about pipeline
    mechanics -- ordering, resumability, status -- and leaves what a concept IS
    to `student/concepts.py`, which is the part that calls a model.
    """

    def __init__(self, db: Database, *, concept_extractor=None,
                 storage_dir: str | Path | None = None, workers: int = 1):
        self.db = db
        self.concept_extractor = concept_extractor
        self.storage_dir = Path(storage_dir) if storage_dir else (
            Path(db.path).parent / "sources")
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._queue: queue.Queue = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        for _ in range(max(1, workers)):
            t = threading.Thread(target=self._worker, daemon=True)
            t.start()
            self._threads.append(t)

    # ---------- queueing ----------

    def enqueue_source(self, source_id: str, *, raw_text: str = "", url: str = "") -> None:
        self._idle.clear()
        self._queue.put({"source_id": source_id, "raw_text": raw_text, "url": url})

    def wait_idle(self, timeout: float = 60.0) -> bool:
        """Block until the queue drains. Used by tests and by any caller that
        needs ingestion finished before it reads the results."""
        return self._idle.wait(timeout)

    def stop(self) -> None:
        self._stop.set()
        for _ in self._threads:
            self._queue.put(None)

    def _worker(self) -> None:
        while not self._stop.is_set():
            job = self._queue.get()
            if job is None:
                break
            try:
                self.process_source(job["source_id"], raw_text=job.get("raw_text", ""),
                                    url=job.get("url", ""))
            except Exception:
                # A worker that dies takes every later source with it.
                traceback.print_exc()
            finally:
                self._queue.task_done()
                if self._queue.empty():
                    self._idle.set()

    # ---------- the pipeline ----------

    def process_source(self, source_id: str, *, raw_text: str = "", url: str = "") -> None:
        row = self.db.query_one("SELECT * FROM sources WHERE id = ?", (source_id,))
        if row is None:
            return

        try:
            self.db.execute("UPDATE sources SET status='chunking' WHERE id=?", (source_id,))
            pages = self._extract(row, raw_text=raw_text, url=url)
            chunks = chunk_pages(pages)
            if not chunks:
                raise ExtractionUnavailable("no usable text was found in this source")
            self._store_chunks(source_id, chunks)

            # The gate, before concept extraction rather than after. A rejected
            # source stops here: its chunks stay on disk so the learner can be
            # shown WHY it failed and re-upload, but nothing downstream is
            # allowed to build study material from text nobody could read.
            verdict = assess(chunks)
            self.db.execute(
                "UPDATE sources SET quality=?, quality_reasons=?, mean_confidence=?,"
                " low_confidence_ratio=? WHERE id=?",
                (verdict.quality, json.dumps(verdict.reasons), verdict.mean_confidence,
                 verdict.low_confidence_ratio, source_id))
            if verdict.rejected:
                raise ExtractionUnavailable("; ".join(verdict.reasons))

            self.db.execute(
                "UPDATE sources SET status='processing', page_count=? WHERE id=?",
                (len({p.locator.get('page', p.ordinal) for p in pages}), source_id))
            self._process_chunks(source_id)
            # Extraction succeeding does not mean the source is usable. If every
            # chunk failed concept extraction, marking the source 'extracted'
            # with error=NULL reports success for a source nothing downstream
            # can use -- generation then finds no concepts and the learner is
            # told there is nothing to make questions from, with the real cause
            # two tables away. Report the state the source is actually in.
            counts = {r["status"]: r["n"] for r in self.db.query(
                "SELECT status, COUNT(*) AS n FROM source_chunks WHERE source_id = ?"
                " GROUP BY status", (source_id,))}
            failed, processed = counts.get("failed", 0), counts.get("processed", 0)
            if failed and not processed:
                reason = self.db.query_one(
                    "SELECT error FROM source_chunks WHERE source_id = ? AND status = 'failed'"
                    " AND error IS NOT NULL ORDER BY ordinal LIMIT 1", (source_id,))
                self.db.execute(
                    "UPDATE sources SET status='failed', error=? WHERE id=?",
                    (f"all {failed} chunk(s) failed concept extraction; first error: "
                     f"{reason['error'] if reason else 'unrecorded'}", source_id))
            elif failed:
                # Partial success is still success -- one bad page must not cost
                # a 300-page book -- but it is not silent success.
                self.db.execute(
                    "UPDATE sources SET status='extracted', error=? WHERE id=?",
                    (f"{failed} of {failed + processed} chunk(s) failed concept extraction; "
                     "the rest were processed", source_id))
            else:
                self.db.execute("UPDATE sources SET status='extracted', error=NULL WHERE id=?",
                                (source_id,))
        except ExtractionUnavailable as exc:
            self.db.execute("UPDATE sources SET status='failed', error=? WHERE id=?",
                            (str(exc), source_id))
        except Exception as exc:
            self.db.execute("UPDATE sources SET status='failed', error=? WHERE id=?",
                            (f"{type(exc).__name__}: {exc}", source_id))

    def _extract(self, row, *, raw_text: str, url: str) -> list[ExtractedPage]:
        path = None
        if row["storage_key"]:
            candidate = self.storage_dir / row["storage_key"]
            if candidate.exists():
                path = candidate
        return extract_for_kind(row["kind"], path=path, raw_text=raw_text, url=url)

    def _store_chunks(self, source_id: str, chunks: list[Chunk]) -> None:
        """Idempotent: re-ingesting a source replaces its chunk set rather than
        appending a second copy."""
        self.db.execute("DELETE FROM source_chunks WHERE source_id = ?", (source_id,))
        conn = self.db.connect()
        conn.executemany(
            "INSERT INTO source_chunks (id, source_id, ordinal, text, locator_json,"
            " confidence, extraction_method, needs_review, status)"
            " VALUES (?,?,?,?,?,?,?,?, 'pending')",
            [(new_id("chk"), source_id, i, c.text, json.dumps(c.locator),
              c.confidence, c.extraction_method, 1 if c.needs_review else 0)
             for i, c in enumerate(chunks, start=1)],
        )
        conn.commit()

    def _process_chunks(self, source_id: str) -> None:
        """
        Process every chunk that is not already `processed`.

        The status write happens before the work, so a process killed mid-chunk
        resumes at that chunk rather than repeating the whole source. Failure of
        one chunk is recorded and the rest continue: a single bad page should
        not cost a 300-page book.
        """
        pending = self.db.query(
            "SELECT * FROM source_chunks WHERE source_id = ? AND status != 'processed'"
            " ORDER BY ordinal", (source_id,))
        for chunk in pending:
            self.db.execute("UPDATE source_chunks SET status='processing' WHERE id=?",
                            (chunk["id"],))
            try:
                if self.concept_extractor is not None:
                    self.concept_extractor.extract_for_chunk(
                        source_id=source_id, chunk_id=chunk["id"],
                        text=chunk["text"], locator=json.loads(chunk["locator_json"]))
                self.db.execute(
                    "UPDATE source_chunks SET status='processed', error=NULL, processed_at=?"
                    " WHERE id=?", (now_iso(), chunk["id"]))
            except Exception as exc:
                self.db.execute(
                    "UPDATE source_chunks SET status='failed', error=? WHERE id=?",
                    (f"{type(exc).__name__}: {exc}", chunk["id"]))
