"""
Attempts, the R/O/G knowledge state, and knowledge gaps.

## The learner owns the colour

The system never infers red/orange/green from correctness. `record_attempt`
requires an explicit `user_colour` and stores it unchanged. Correctness is
recorded alongside it, and the two are kept separate for the whole life of the
data, because they answer different questions: *was this right* and *did I know
it*. A lucky guess is correct and RED; a careless slip is wrong and GREEN.

## One wrong answer does not condemn a concept

A question can be missed for reasons that say nothing about the concept --
misreading the stem, unfamiliar framing, confusion between two neighbours,
simple carelessness. So concept colour accumulates evidence rather than
mirroring the last attempt:

    RED     two or more REDs in the last five attempts on this concept
    GREEN   no RED in that window, and the two most recent are GREEN
    ORANGE  everything else, including a single recent RED

A single RED therefore moves a concept to ORANGE, not RED. Being wrong once is
uncertainty; being wrong repeatedly is a gap.

## Gaps are specific, and they point back at their evidence

`knowledge_gaps` stores the named thing a learner is missing -- "ferritin
interpretation", not "anaemia" -- and `gap_links` ties each occurrence to the
concept, question, attempt, notebook, source and chunk that produced it. That
is what makes the Weak screen able to offer a source jump per gap rather than a
vague topic label.
"""

from __future__ import annotations

import json

from .concepts import normalize
from .db import Database, new_id, now_iso

RED, ORANGE, GREEN = "RED", "ORANGE", "GREEN"
COLOURS = (RED, ORANGE, GREEN)

# How far back the concept-colour rule looks. Five is enough for a repeated
# failure to show up and short enough that old evidence stops dominating a
# concept the learner has since fixed.
EVIDENCE_WINDOW = 5


def derive_concept_colour(recent_colours: list[str]) -> str:
    """
    Concept colour from the learner's own recent colours, newest first.

    Deterministic and explainable on purpose: a learner who asks "why is this
    red" gets an answer made of their own judgements, not a model's opinion.
    """
    window = [c for c in recent_colours if c in COLOURS][:EVIDENCE_WINDOW]
    if not window:
        return ORANGE            # unknown, not "fine"
    if window.count(RED) >= 2:
        return RED
    if window.count(RED) == 0 and window[:2] == [GREEN, GREEN]:
        return GREEN
    return ORANGE


class KnowledgeStore:
    def __init__(self, db: Database):
        self.db = db

    # ---------- attempts ----------

    def record_attempt(self, *, user_id: str, question_id: str, user_answer: int | None,
                       user_colour: str, session_id: str | None = None,
                       gaps: list[str] | None = None) -> dict:
        if user_colour not in COLOURS:
            raise ValueError(
                f"user_colour must be one of {COLOURS}; the learner chooses it and the "
                "system never infers it")

        question = self.db.query_one("SELECT * FROM questions WHERE id = ?", (question_id,))
        if question is None:
            raise ValueError(f"no such question: {question_id}")

        correct_index = question["correct_index"]
        is_correct = 1 if user_answer == correct_index else 0

        concept_rows = self.db.query(
            "SELECT concept_id FROM question_concepts WHERE question_id = ?", (question_id,))
        concept_ids = [r["concept_id"] for r in concept_rows]

        source_refs = []
        if question["source_id"]:
            source_refs.append({"source_id": question["source_id"],
                                "chunk_id": question["chunk_id"]})

        attempt_id = new_id("att")
        self.db.execute(
            "INSERT INTO attempts (id, question_id, session_id, user_id, user_answer,"
            " correct_answer, is_correct, user_colour, concepts_tested_json,"
            " knowledge_gaps_json, source_refs_json, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (attempt_id, question_id, session_id, user_id, user_answer, correct_index,
             is_correct, user_colour, json.dumps(concept_ids), json.dumps(gaps or []),
             json.dumps(source_refs), now_iso()))

        for cid in concept_ids:
            self._refresh_concept_state(user_id, cid)
        for label in gaps or []:
            self._record_gap(user_id, label, attempt_id, question, concept_ids, user_colour)
        self._update_revision_state(user_id, question_id, is_correct, user_colour)

        return {"attempt_id": attempt_id, "is_correct": bool(is_correct),
                "correct_answer": correct_index, "user_colour": user_colour,
                "concepts_tested": concept_ids}

    # ---------- concept state ----------

    def _refresh_concept_state(self, user_id: str, concept_id: str) -> None:
        rows = self.db.query(
            """SELECT a.user_colour, a.is_correct, a.created_at FROM attempts a
                 JOIN question_concepts qc ON qc.question_id = a.question_id
                WHERE a.user_id = ? AND qc.concept_id = ?
                ORDER BY a.created_at DESC, a.id DESC""",
            (user_id, concept_id))
        colours = [r["user_colour"] for r in rows]
        correct = sum(1 for r in rows if r["is_correct"])
        wrong = len(rows) - correct

        streak = 0
        for r in rows:
            if r["is_correct"]:
                streak += 1
            else:
                break

        self.db.execute(
            "INSERT INTO concept_state (user_id, concept_id, colour, correct_count,"
            " wrong_count, consecutive_correct, last_seen_at) VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(user_id, concept_id) DO UPDATE SET colour=excluded.colour,"
            " correct_count=excluded.correct_count, wrong_count=excluded.wrong_count,"
            " consecutive_correct=excluded.consecutive_correct,"
            " last_seen_at=excluded.last_seen_at",
            (user_id, concept_id, derive_concept_colour(colours), correct, wrong,
             streak, rows[0]["created_at"] if rows else now_iso()))

    def concept_state(self, user_id: str, concept_id: str) -> dict | None:
        row = self.db.query_one(
            "SELECT * FROM concept_state WHERE user_id = ? AND concept_id = ?",
            (user_id, concept_id))
        return dict(row) if row else None

    # ---------- gaps ----------

    def _record_gap(self, user_id: str, label: str, attempt_id: str, question,
                    concept_ids: list[str], colour: str) -> str:
        label = (label or "").strip()
        if not label:
            return ""
        key = normalize(label)
        existing = self.db.query_one(
            "SELECT id FROM knowledge_gaps WHERE user_id = ? AND normalized = ?",
            (user_id, key))

        if existing:
            gap_id = existing["id"]
            # Seeing a gap again reopens it: `resolved_at` is cleared, because
            # a gap that recurs was not resolved.
            self.db.execute(
                "UPDATE knowledge_gaps SET last_seen_at = ?, colour = ?, resolved_at = NULL"
                " WHERE id = ?", (now_iso(), colour, gap_id))
        else:
            gap_id = new_id("gap")
            self.db.execute(
                "INSERT INTO knowledge_gaps (id, user_id, label, normalized, concept_id,"
                " colour, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?)",
                (gap_id, user_id, label, key, concept_ids[0] if concept_ids else None,
                 colour, now_iso(), now_iso()))

        self.db.execute(
            "INSERT OR IGNORE INTO gap_links (gap_id, question_id, attempt_id, notebook_id,"
            " source_id, chunk_id) VALUES (?,?,?,?,?,?)",
            (gap_id, question["id"], attempt_id, question["primary_notebook_id"],
             question["source_id"], question["chunk_id"]))
        return gap_id

    def resolve_gap(self, user_id: str, gap_id: str) -> None:
        self.db.execute(
            "UPDATE knowledge_gaps SET resolved_at = ? WHERE id = ? AND user_id = ?",
            (now_iso(), gap_id, user_id))

    def gaps(self, user_id: str, *, colour: str | None = None,
             include_resolved: bool = False) -> list[dict]:
        sql = ["SELECT g.*, c.canonical_name AS concept_name,",
               " (SELECT COUNT(*) FROM gap_links l WHERE l.gap_id = g.id) AS evidence_count",
               " FROM knowledge_gaps g LEFT JOIN concepts c ON c.id = g.concept_id",
               " WHERE g.user_id = ?"]
        params: list = [user_id]
        if not include_resolved:
            sql.append(" AND g.resolved_at IS NULL")
        if colour:
            sql.append(" AND g.colour = ?")
            params.append(colour)
        sql.append(" ORDER BY g.last_seen_at DESC")
        return [dict(r) for r in self.db.query("".join(sql), tuple(params))]

    def gap_evidence(self, user_id: str, gap_id: str) -> list[dict]:
        """
        Every question, attempt, notebook and source passage behind one gap.

        This is what turns "you are weak at anaemia" into "these four questions,
        in these two notebooks, from this passage".
        """
        rows = self.db.query(
            """SELECT l.question_id, l.attempt_id, l.notebook_id, l.source_id, l.chunk_id,
                      q.stem, n.title AS notebook_title, a.is_correct, a.user_colour,
                      a.created_at, ch.text AS passage, ch.locator_json
                 FROM gap_links l
                 JOIN knowledge_gaps g ON g.id = l.gap_id AND g.user_id = ?
                 LEFT JOIN questions q ON q.id = l.question_id
                 LEFT JOIN notebooks n ON n.id = l.notebook_id
                 LEFT JOIN attempts a ON a.id = l.attempt_id
                 LEFT JOIN source_chunks ch ON ch.id = l.chunk_id
                WHERE l.gap_id = ? ORDER BY a.created_at DESC""",
            (user_id, gap_id))
        return [dict(r) for r in rows]

    # ---------- spaced repetition ----------

    def _update_revision_state(self, user_id: str, question_id: str, is_correct: int,
                              colour: str) -> None:
        """
        SM-2, with the learner's colour as the grade.

        The colour is used rather than raw correctness because it is the better
        signal: a guessed-correct answer graded RED should come back soon, and
        SM-2 driven by correctness alone would push it weeks away.
        """
        row = self.db.query_one(
            "SELECT * FROM revision_state WHERE user_id = ? AND question_id = ?",
            (user_id, question_id))
        ease = row["ease_factor"] if row else 2.5
        interval = row["interval_days"] if row else 0.0
        streak = row["consecutive_correct"] if row else 0

        quality = {RED: 2, ORANGE: 3, GREEN: 5}[colour]
        if not is_correct:
            quality = min(quality, 2)

        if quality < 3:
            streak, interval = 0, 1.0
        else:
            streak += 1
            interval = 1.0 if streak == 1 else 6.0 if streak == 2 else round(interval * ease, 2)
        ease = max(1.3, ease + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02)))

        from datetime import datetime, timedelta, timezone
        due = (datetime.now(timezone.utc) + timedelta(days=interval)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")

        if row:
            self.db.execute(
                "UPDATE revision_state SET ease_factor=?, interval_days=?, due_at=?,"
                " last_reviewed_at=?, last_result=?, consecutive_correct=? WHERE id=?",
                (ease, interval, due, now_iso(), colour, streak, row["id"]))
        else:
            self.db.execute(
                "INSERT INTO revision_state (id, user_id, question_id, ease_factor,"
                " interval_days, due_at, last_reviewed_at, last_result, consecutive_correct)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (new_id("rs"), user_id, question_id, ease, interval, due, now_iso(),
                 colour, streak))

    # ---------- rollups ----------

    def colour_counts(self, user_id: str) -> dict:
        rows = self.db.query(
            "SELECT colour, COUNT(*) n FROM concept_state WHERE user_id = ? GROUP BY colour",
            (user_id,))
        counts = {c: 0 for c in COLOURS}
        counts.update({r["colour"]: r["n"] for r in rows})
        return counts

    def due_count(self, user_id: str) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) n FROM revision_state WHERE user_id = ? AND due_at <= ?",
            (user_id, now_iso()))
        return row["n"] if row else 0
