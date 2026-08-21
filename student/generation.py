"""
Question generation, and the AI concept extractor that completes ingestion.

## What the generator is given

Never "write me an MCQ". Every generation call carries, per
`docs/QUINTEK_LOGIC.md` section 4.2:

    source passages (grounding) + target concepts + related concepts
      + cross-notebook context + question type + difficulty
      + reasoning depth + demonstration examples + user constraints

## Demonstrations supply structure, never facts

A demo is an exemplar of *shape*: stem structure, distractor strategy, answer
format, reasoning depth. The prompt says so explicitly and repeats it, because
the failure mode is a model lifting a clinical value out of a demo and asserting
it about the learner's material -- a fabricated fact wearing the costume of a
grounded one.

## Everything generated keeps its provenance

`source_id`, `chunk_id`, `generated_by_candidate_id`, `prompt_version` and
`demo_ids` are stored on every question. That is what makes "show me where this
came from" answerable, and it is impossible to backfill.
"""

from __future__ import annotations

import json

from .ai import AIEngine, extract_json
from .concepts import ConceptStore
from .db import Database, new_id, now_iso

GENERATION_PROMPT_VERSION = "gen-v1"
EXTRACTION_PROMPT_VERSION = "ext-v1"

_GROUNDING_RULE = (
    "Every question must be answerable from the SOURCE PASSAGES alone. Do not "
    "introduce a fact, value, threshold or guideline that does not appear in "
    "them. If the passages do not support a question at the requested "
    "difficulty, return fewer questions rather than inventing material."
)

_DEMO_RULE = (
    "The DEMONSTRATIONS show STYLE ONLY -- stem structure, how distractors are "
    "built, answer format, depth of reasoning. Never reuse a clinical fact, "
    "number, drug or diagnosis from a demonstration. Their content is "
    "irrelevant to this task; only their shape matters."
)


class GenerationFailed(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Concept extraction (the AI half of ingestion)
# ---------------------------------------------------------------------------

class AIConceptExtractor:
    """
    Pulls concepts and relationships out of one chunk.

    Plugged into `IngestionEngine`, which owns ordering and resumability. This
    class owns only "what is in this text", and resolution stays in
    `ConceptStore`, which refuses to merge on similarity.
    """

    def __init__(self, db: Database, ai: AIEngine):
        self.db = db
        self.ai = ai
        self.store = ConceptStore(db)

    def extract_for_chunk(self, *, source_id: str, chunk_id: str, text: str,
                          locator: dict) -> dict:
        notebook = self.db.query_one(
            "SELECT n.id, n.subject FROM sources s JOIN notebooks n ON n.id = s.notebook_id"
            " WHERE s.id = ?", (source_id,))
        subject = notebook["subject"] if notebook else ""

        prompt = (
            "Extract the medical concepts this passage TEACHES, and the "
            "relationships between them.\n\n"
            "Rules:\n"
            "- Only concepts the passage actually explains or uses substantively. "
            "A word mentioned in passing is not a concept.\n"
            "- Use the canonical clinical name, not the passage's phrasing.\n"
            "- Do not split one concept into near-duplicates.\n"
            f"- Allowed relation types: {', '.join(sorted(_RELATIONS))}.\n\n"
            "Reply with ONLY a JSON object:\n"
            '{"concepts": [{"name": "...", "description": "..."}], '
            '"relationships": [{"from": "...", "to": "...", "type": "...", '
            '"confidence": 0.0}]}\n\n'
            f"PASSAGE:\n{text}"
        )
        result = self.ai.call("CONCEPT_EXTRACTION", prompt,
                              prompt_version=EXTRACTION_PROMPT_VERSION,
                              max_tokens=900, temperature=0.0)
        payload = result.parsed or extract_json(result.text) or {}

        created: dict[str, str] = {}
        for item in payload.get("concepts", []) or []:
            name = (item.get("name") or "").strip()
            if not name:
                continue
            cid = self.store.resolve_or_create(
                name, subject=subject, description=(item.get("description") or "").strip())
            created[name.lower()] = cid
            self.store.link_to_source(source_id, cid, chunk_id)
            if notebook:
                self.store.link_to_notebook(notebook["id"], cid)

        for rel in payload.get("relationships", []) or []:
            a = created.get((rel.get("from") or "").strip().lower())
            b = created.get((rel.get("to") or "").strip().lower())
            rtype = (rel.get("type") or "related_to").strip()
            if not (a and b) or rtype not in _RELATIONS:
                continue
            try:
                self.store.relate(a, b, rtype,
                                  confidence=float(rel.get("confidence") or 0.0),
                                  provenance_source_id=source_id)
            except ValueError:
                continue

        return {"concepts": len(created)}


from .concepts import RELATION_TYPES as _RELATIONS  # noqa: E402  (cycle-free at runtime)


# ---------------------------------------------------------------------------
# Question generation
# ---------------------------------------------------------------------------

def _rejection_reason(item: dict) -> str:
    """
    Why `_store_question` would refuse this item.

    Deliberately mirrors that method's checks in the same order. Kept as a
    separate function rather than returned from `_store_question` so the
    storing path stays a simple "id or None" -- but it means the two must be
    changed together, and `test_generation_trace.py` asserts they agree.
    """
    stem = (item.get("stem") or "").strip()
    options = [str(o).strip() for o in (item.get("options") or []) if str(o).strip()]
    try:
        correct = int(item.get("correct_index"))
    except (TypeError, ValueError):
        return "correct_index is missing or not an integer"
    if not stem:
        return "stem is empty"
    if len(options) < 2:
        return f"only {len(options)} usable option(s); at least 2 are needed"
    if not (0 <= correct < len(options)):
        return (f"correct_index {correct} points outside the {len(options)} options -- "
                "a key that points nowhere can never be answered")
    return "unknown"


class QuestionGenerator:
    def __init__(self, db: Database, ai: AIEngine):
        self.db = db
        self.ai = ai
        self.store = ConceptStore(db)

    # -- context assembly --

    def _passages(self, source_id: str | None, concept_ids: list[str],
                  limit: int = 6, notebook_id: str | None = None) -> list[dict]:
        """
        Grounding text, narrowest source first: the chunks that mention the
        target concepts, else the named source, else anything ingested into
        this notebook.

        The notebook fallback matters because "make questions from this
        notebook" -- no concept, no source named -- is the ordinary request,
        and without it the common case looks like an ungrounded one.
        """
        if concept_ids:
            marks = ",".join("?" for _ in concept_ids)
            rows = self.db.query(
                f"""SELECT DISTINCT ch.id, ch.text, ch.locator_json, ch.source_id
                      FROM source_concepts sc JOIN source_chunks ch ON ch.id = sc.chunk_id
                     WHERE sc.concept_id IN ({marks}) ORDER BY ch.ordinal LIMIT ?""",
                (*concept_ids, limit))
            if rows:
                return [dict(r) for r in rows]
        if source_id:
            rows = self.db.query(
                "SELECT id, text, locator_json, source_id FROM source_chunks"
                " WHERE source_id = ? ORDER BY ordinal LIMIT ?", (source_id, limit))
            if rows:
                return [dict(r) for r in rows]
        if notebook_id:
            rows = self.db.query(
                """SELECT ch.id, ch.text, ch.locator_json, ch.source_id
                     FROM source_chunks ch JOIN sources s ON s.id = ch.source_id
                    WHERE s.notebook_id = ? AND ch.status = 'processed'
                    ORDER BY ch.ordinal LIMIT ?""", (notebook_id, limit))
            return [dict(r) for r in rows]
        return []

    def _related(self, concept_ids: list[str], limit: int = 10) -> list[str]:
        names: list[str] = []
        for cid in concept_ids:
            for n in self.store.neighbours(cid):
                if n["canonical_name"] not in names:
                    names.append(n["canonical_name"])
        return names[:limit]

    def _demos(self, demo_ids: list[str], owner_id: str = "") -> list[dict]:
        """
        Style references, scoped to their OWNER.

        The ids arrive from the client -- the Make Questions screen sends
        whichever the learner selected -- so an unscoped lookup would let
        anyone read anyone else's reference question by guessing an id, and
        read it through a channel that puts the text straight into a prompt.
        A demonstration is something a learner wrote; it is theirs.

        `owner_id` is optional only so existing internal callers that already
        resolved ownership keep working. Every path reachable from HTTP
        passes it.
        """
        if not demo_ids:
            return []
        marks = ",".join("?" for _ in demo_ids)
        if owner_id:
            return [dict(r) for r in self.db.query(
                f"SELECT * FROM question_demos WHERE id IN ({marks})"
                f" AND owner_id = ?", tuple(demo_ids) + (owner_id,))]
        return [dict(r) for r in self.db.query(
            f"SELECT * FROM question_demos WHERE id IN ({marks})", tuple(demo_ids))]

    def build_prompt(self, *, count: int, passages: list[dict], target_names: list[str],
                     related_names: list[str], demos: list[dict], family: str,
                     difficulty: str, reasoning_depth: str, constraints: str) -> str:
        parts = [
            f"Write {count} postgraduate-level medical question(s).",
            "",
            "GROUNDING RULE", _GROUNDING_RULE, "",
            "SOURCE PASSAGES",
        ]
        for i, p in enumerate(passages, start=1):
            loc = json.loads(p["locator_json"]) if p.get("locator_json") else {}
            parts.append(f"[passage {i} | {json.dumps(loc)}]\n{p['text']}")
        parts += ["", f"TARGET CONCEPTS: {', '.join(target_names) or '(any in the passages)'}"]
        if related_names:
            parts.append(f"RELATED CONCEPTS (may be integrated): {', '.join(related_names)}")
        parts += [
            "",
            f"QUESTION TYPE: {family or 'single best answer'}",
            f"DIFFICULTY: {difficulty or 'postgraduate'}",
            f"REASONING DEPTH: {reasoning_depth or 'requires integrating two concepts'}",
        ]
        if constraints:
            parts.append(f"ADDITIONAL CONSTRAINTS: {constraints}")

        if demos:
            parts += ["", "DEMONSTRATIONS", _DEMO_RULE]
            for d in demos:
                parts.append(
                    f"- {d['title']}: structure={d['stem_structure'] or 'n/a'}; "
                    f"target={d['question_target'] or 'n/a'}; "
                    f"distractors={d['distractor_strategy'] or 'n/a'}; "
                    f"format={d['answer_format'] or 'n/a'}\n  example: {d['question']}")
            parts.append(_DEMO_RULE)   # repeated: this is the fact-leak failure mode

        parts += [
            "",
            "Reply with ONLY a JSON object:",
            '{"questions": [{"stem": "...", "options": ["...","...","...","..."], '
            '"correct_index": 0, "rationale": "...", "concepts_tested": ["..."], '
            '"passage": 1}]}',
            "`passage` is the number of the SOURCE PASSAGE the question is answerable from.",
        ]
        return "\n".join(parts)

    def generate(self, *, notebook_id: str, count: int = 5,
                 concept_ids: list[str] | None = None, source_id: str | None = None,
                 demo_ids: list[str] | None = None, family: str = "",
                 difficulty: str = "", reasoning_depth: str = "",
                 constraints: str = "", owner_id: str = "", trace=None) -> list[str]:
        """
        `trace` is a `student.trace.GenerationTrace`, or None for no capture.

        Tracing is threaded through rather than bolted on afterwards because
        the artifacts worth having -- the exact passages retrieved, the exact
        prompt, the raw bytes returned, and what was dropped during
        normalization -- only exist inside this method. Reconstructing them
        from the stored question afterwards is guesswork.
        """
        from .trace import NullTrace

        trace = trace or NullTrace()
        concept_ids = concept_ids or []
        demo_ids = demo_ids or []
        if count < 1:
            raise GenerationFailed("count must be at least 1")

        trace.run_started(notebook_id=notebook_id, requested_count=count,
                          concept_ids=concept_ids, source_id=source_id,
                          demo_ids=demo_ids, family=family, difficulty=difficulty,
                          reasoning_depth=reasoning_depth)

        passages = self._passages(source_id, concept_ids, notebook_id=notebook_id)
        if not passages:
            # Ungrounded generation is exactly the thing the grounding rule
            # forbids; refusing is better than producing plausible invention.
            failure = GenerationFailed(
                "no source passages are available to ground these questions -- "
                "ingest a source first")
            trace.failed("retrieval", failure)
            raise failure

        source_row = self.db.query_one(
            "SELECT * FROM sources WHERE id = ?", (passages[0]["source_id"],)) \
            if passages[0].get("source_id") else None
        trace.source(dict(source_row) if source_row else {}, passages)

        targets = [self.store.get(c)["canonical_name"] for c in concept_ids
                   if self.store.get(c)]
        trace.concepts(
            [{"concept_id": c, "canonical_name": n} for c, n in zip(concept_ids, targets)],
            resolution=[{"related": self._related(concept_ids)}])

        prompt = self.build_prompt(
            count=count, passages=passages, target_names=targets,
            related_names=self._related(concept_ids),
            demos=self._demos(demo_ids, owner_id),
            family=family, difficulty=difficulty, reasoning_depth=reasoning_depth,
            constraints=constraints)
        trace.prompt(prompt=prompt, task_type="QUESTION_GENERATION",
                     prompt_version=GENERATION_PROMPT_VERSION, temperature=0.2,
                     max_tokens=400 * count + 600, demos=demo_ids)

        try:
            result = self.ai.call("QUESTION_GENERATION", prompt,
                                  prompt_version=GENERATION_PROMPT_VERSION,
                                  max_tokens=400 * count + 600, temperature=0.2)
        except Exception as exc:
            trace.failed("model_call", exc)
            raise
        trace.raw_output(result)

        payload = result.parsed or extract_json(result.text) or {}
        raw = payload.get("questions") or []
        if not raw:
            failure = GenerationFailed("the model returned no questions")
            trace.normalized(accepted=[], rejected=[
                {"reason": "no 'questions' array in the reply", "payload_keys": sorted(payload)}])
            trace.failed("normalization", failure)
            raise failure

        created: list[str] = []
        accepted, rejected = [], []
        for item in raw[:count]:
            qid = self._store_question(item, notebook_id, passages, result, demo_ids,
                                       family, difficulty, reasoning_depth)
            if qid:
                created.append(qid)
                accepted.append({"question_id": qid, "item": item})
            else:
                # Why it was dropped, not just that it was.
                rejected.append({"item": item, "reason": _rejection_reason(item)})
        trace.normalized(accepted=accepted, rejected=rejected)

        if not created:
            failure = GenerationFailed(
                "no returned question was well-formed enough to store")
            trace.failed("normalization", failure)
            raise failure
        trace.final(decision="stored", reason=f"{len(created)} of {len(raw)} stored",
                    question_ids=created, dropped=len(rejected))
        return created

    def _store_question(self, item: dict, notebook_id: str, passages: list[dict],
                        result, demo_ids: list[str], family: str, difficulty: str,
                        reasoning_depth: str) -> str | None:
        stem = (item.get("stem") or "").strip()
        options = [str(o).strip() for o in (item.get("options") or []) if str(o).strip()]
        try:
            correct = int(item.get("correct_index"))
        except (TypeError, ValueError):
            return None
        # A malformed question is dropped rather than stored broken: a question
        # whose key points outside its own options can never be answered.
        if not stem or len(options) < 2 or not (0 <= correct < len(options)):
            return None

        idx = item.get("passage")
        chunk = None
        if isinstance(idx, int) and 1 <= idx <= len(passages):
            chunk = passages[idx - 1]
        elif passages:
            chunk = passages[0]

        qid = new_id("q")
        self.db.execute(
            "INSERT INTO questions (id, primary_notebook_id, family, stem, options_json,"
            " correct_index, rationale, difficulty, reasoning_depth, source_id, chunk_id,"
            " generated_by_candidate_id, prompt_version, demo_ids_json, validation_status,"
            " generated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending', ?)",
            (qid, notebook_id, family, stem, json.dumps(options), correct,
             (item.get("rationale") or "").strip(), difficulty, reasoning_depth,
             chunk["source_id"] if chunk else None, chunk["id"] if chunk else None,
             result.candidate_id, GENERATION_PROMPT_VERSION, json.dumps(demo_ids),
             now_iso()))

        for name in item.get("concepts_tested") or []:
            cid = self.store.find(str(name))
            if cid:
                self.db.execute(
                    "INSERT OR IGNORE INTO question_concepts (question_id, concept_id, role)"
                    " VALUES (?,?, 'target')", (qid, cid))
        return qid
