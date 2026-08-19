"""
Concept priority and the adaptive revision engine.

## Priority is code, not an opinion

Ranking is computed from stored signals only -- colour, wrong frequency,
repeated failure, unresolved gaps, recency, overdue interval, retrieval streak,
improvement trend, question coverage. No model is consulted.

Two reasons that matters. First, the same inputs must always produce the same
order; a learner who reopens the app should not find their revision list
reshuffled because a model sampled differently. Second, the concept screen has
to explain *why* a concept sits where it does, and an explanation assembled
from named signals is checkable in a way "the model thought so" is not.

## Selection order

From `docs/QUINTEK_LOGIC.md` section 6:

    1 RED knowledge gaps          5 previously incorrect questions
    2 RED concepts                6 due concepts
    3 ORANGE knowledge gaps       7 full-section coverage
    4 ORANGE concepts             8 unseen questions

Two behaviours the order alone does not capture:

  * **Do not just re-serve the failed question.** A learner who has memorised
    that D is the answer has learned the question, not the concept. New items
    on the same concept are preferred, and the original is only reused when
    nothing else tests that concept.

  * **Weak *plus* full section.** If `ferritin interpretation` is RED, the
    session pulls ferritin interpretation, iron studies, iron metabolism and
    neighbouring concepts -- targeted weakness and the section around it, not a
    drill on one item.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .concepts import ConceptStore
from .db import Database, new_id, now_iso
from .knowledge import GREEN, ORANGE, RED, KnowledgeStore

# Weights for the priority score. Named constants rather than inline numbers so
# the concept screen can print the same names it scored with.
W_COLOUR = {RED: 100.0, ORANGE: 45.0, GREEN: 0.0}
W_WRONG_EACH = 8.0
W_REPEATED_FAILURE = 25.0      # two or more wrongs is a pattern, not an accident
W_UNRESOLVED_GAP = 30.0
W_OVERDUE_PER_DAY = 4.0
W_STREAK_EACH = -6.0           # retrieval success genuinely lowers priority
W_IMPROVING = -15.0
W_NEVER_TESTED = 12.0          # unmeasured is not the same as fine
MAX_OVERDUE_BONUS = 40.0


class PriorityEngine:
    def __init__(self, db: Database):
        self.db = db
        self.knowledge = KnowledgeStore(db)

    def _signals(self, user_id: str) -> list[dict]:
        rows = self.db.query(
            """
            SELECT c.id AS concept_id, c.canonical_name, c.subject,
                   COALESCE(cs.colour, 'ORANGE')        AS colour,
                   COALESCE(cs.correct_count, 0)        AS correct_count,
                   COALESCE(cs.wrong_count, 0)          AS wrong_count,
                   COALESCE(cs.consecutive_correct, 0)  AS consecutive_correct,
                   cs.last_seen_at,
                   (SELECT COUNT(*) FROM knowledge_gaps g
                     WHERE g.user_id = ? AND g.concept_id = c.id
                       AND g.resolved_at IS NULL)       AS open_gaps,
                   (SELECT COUNT(*) FROM question_concepts qc
                     WHERE qc.concept_id = c.id)        AS question_count,
                   (SELECT MIN(rs.due_at) FROM revision_state rs
                      JOIN question_concepts qc2 ON qc2.question_id = rs.question_id
                     WHERE rs.user_id = ? AND qc2.concept_id = c.id) AS next_due
              FROM concepts c
              JOIN notebook_concepts nc ON nc.concept_id = c.id
              JOIN notebooks n ON n.id = nc.notebook_id AND n.owner_id = ?
              LEFT JOIN concept_state cs ON cs.concept_id = c.id AND cs.user_id = ?
             GROUP BY c.id
            """, (user_id, user_id, user_id, user_id))
        return [dict(r) for r in rows]

    def _overdue_days(self, next_due: str | None) -> float:
        if not next_due:
            return 0.0
        try:
            due = datetime.strptime(next_due, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            return 0.0
        delta = (datetime.now(timezone.utc) - due).total_seconds() / 86400.0
        return max(0.0, delta)

    def score_concept(self, row: dict) -> tuple[float, list[dict]]:
        """Score plus the reasons that produced it, so the UI can show its work."""
        reasons: list[dict] = []
        score = 0.0

        colour_weight = W_COLOUR.get(row["colour"], 0.0)
        if colour_weight:
            score += colour_weight
            reasons.append({"signal": "colour", "value": row["colour"],
                            "points": colour_weight})

        if row["wrong_count"]:
            pts = W_WRONG_EACH * row["wrong_count"]
            score += pts
            reasons.append({"signal": "wrong answers", "value": row["wrong_count"],
                            "points": pts})

        if row["wrong_count"] >= 2:
            score += W_REPEATED_FAILURE
            reasons.append({"signal": "repeated failure", "value": row["wrong_count"],
                            "points": W_REPEATED_FAILURE})

        if row["open_gaps"]:
            pts = W_UNRESOLVED_GAP * row["open_gaps"]
            score += pts
            reasons.append({"signal": "unresolved gaps", "value": row["open_gaps"],
                            "points": pts})

        overdue = self._overdue_days(row.get("next_due"))
        if overdue > 0:
            pts = min(MAX_OVERDUE_BONUS, W_OVERDUE_PER_DAY * overdue)
            score += pts
            reasons.append({"signal": "overdue", "value": f"{overdue:.1f} days",
                            "points": round(pts, 1)})

        if row["consecutive_correct"]:
            pts = W_STREAK_EACH * row["consecutive_correct"]
            score += pts
            reasons.append({"signal": "retrieval streak", "value": row["consecutive_correct"],
                            "points": pts})

        # Improving: more right than wrong, and currently on a streak.
        if row["correct_count"] > row["wrong_count"] and row["consecutive_correct"] >= 2:
            score += W_IMPROVING
            reasons.append({"signal": "improving", "value": True, "points": W_IMPROVING})

        if not row["last_seen_at"]:
            score += W_NEVER_TESTED
            reasons.append({"signal": "never tested", "value": True,
                            "points": W_NEVER_TESTED})

        return round(score, 2), reasons

    def ranked(self, user_id: str, limit: int | None = None) -> list[dict]:
        out = []
        for row in self._signals(user_id):
            score, reasons = self.score_concept(row)
            out.append({**row, "priority_score": score, "why": reasons})
        # Ties break on name so the order is stable across calls -- a list that
        # reshuffles between refreshes reads as broken.
        out.sort(key=lambda r: (-r["priority_score"], r["canonical_name"]))
        for i, row in enumerate(out, start=1):
            row["rank"] = i
        return out[:limit] if limit else out

    def explain(self, user_id: str, concept_id: str) -> dict | None:
        for row in self.ranked(user_id):
            if row["concept_id"] == concept_id:
                return row
        return None


class RevisionEngine:
    """Builds a session, and analyses it afterwards."""

    STRATEGIES = ("adaptive", "red", "orange", "green", "due", "unseen")

    def __init__(self, db: Database):
        self.db = db
        self.priority = PriorityEngine(db)
        self.knowledge = KnowledgeStore(db)
        self.concepts = ConceptStore(db)

    # ---------- recommendation ----------

    def dashboard(self, user_id: str) -> dict:
        counts = self.knowledge.colour_counts(user_id)
        gaps = self.knowledge.gaps(user_id)
        red_gaps = [g for g in gaps if g["colour"] == RED]
        due = self.knowledge.due_count(user_id)

        # A recommendation, not a cap: the learner always chooses the count.
        recommended = min(40, max(10, len(red_gaps) * 3 + counts[RED] * 2 + due))
        top = self.priority.ranked(user_id, limit=5)
        return {
            "colour_counts": counts,
            "open_gaps": len(gaps),
            "red_gaps": len(red_gaps),
            "due_count": due,
            "recommended_question_count": recommended,
            "top_priority": [{"concept_id": t["concept_id"], "name": t["canonical_name"],
                              "colour": t["colour"], "score": t["priority_score"],
                              "why": t["why"]} for t in top],
            "strategies": list(self.STRATEGIES),
        }

    # ---------- selection ----------

    def _questions_for_concepts(self, user_id: str, concept_ids: list[str],
                                exclude: set[str]) -> list[str]:
        """
        Approved questions on these concepts, unseen ones first.

        Ordering by attempt count is what implements "do not just re-serve the
        failed question": an item the learner has never seen outranks the one
        they have already learned the answer to.
        """
        if not concept_ids:
            return []
        marks = ",".join("?" for _ in concept_ids)
        rows = self.db.query(
            f"""SELECT q.id,
                       (SELECT COUNT(*) FROM attempts a
                         WHERE a.question_id = q.id AND a.user_id = ?) AS seen
                  FROM questions q JOIN question_concepts qc ON qc.question_id = q.id
                 WHERE qc.concept_id IN ({marks})
                   AND q.validation_status IN ('approved', 'pending')
                 GROUP BY q.id ORDER BY seen ASC, q.generated_at DESC""",
            (user_id, *concept_ids))
        return [r["id"] for r in rows if r["id"] not in exclude]

    def select_questions(self, user_id: str, *, count: int = 20,
                         strategy: str = "adaptive") -> list[str]:
        if strategy not in self.STRATEGIES:
            raise ValueError(f"unknown strategy: {strategy!r}")
        if count < 1:
            raise ValueError("a session needs at least one question")

        chosen: list[str] = []
        seen: set[str] = set()

        def take(question_ids: list[str]) -> None:
            for qid in question_ids:
                if len(chosen) >= count:
                    return
                if qid not in seen:
                    chosen.append(qid)
                    seen.add(qid)

        ranked = self.priority.ranked(user_id)
        by_colour = {c: [r["concept_id"] for r in ranked if r["colour"] == c]
                     for c in (RED, ORANGE, GREEN)}

        if strategy in {"red", "orange", "green"}:
            take(self._questions_for_concepts(user_id, by_colour[strategy.upper()], seen))
            return chosen

        if strategy == "due":
            rows = self.db.query(
                "SELECT question_id FROM revision_state WHERE user_id = ? AND due_at <= ?"
                " ORDER BY due_at", (user_id, now_iso()))
            take([r["question_id"] for r in rows])
            return chosen

        if strategy == "unseen":
            rows = self.db.query(
                """SELECT q.id FROM questions q
                     JOIN notebooks n ON n.id = q.primary_notebook_id AND n.owner_id = ?
                    WHERE NOT EXISTS (SELECT 1 FROM attempts a
                                       WHERE a.question_id = q.id AND a.user_id = ?)
                    ORDER BY q.generated_at DESC""", (user_id, user_id))
            take([r["id"] for r in rows])
            return chosen

        # --- adaptive: the default, in the order from section 6 ---
        gaps = self.knowledge.gaps(user_id)

        def gap_concepts(colour: str) -> list[str]:
            return [g["concept_id"] for g in gaps
                    if g["colour"] == colour and g["concept_id"]]

        take(self._questions_for_concepts(user_id, gap_concepts(RED), seen))       # 1
        take(self._questions_for_concepts(user_id, by_colour[RED], seen))          # 2
        take(self._questions_for_concepts(user_id, gap_concepts(ORANGE), seen))    # 3
        take(self._questions_for_concepts(user_id, by_colour[ORANGE], seen))       # 4

        if len(chosen) < count:                                                     # 5
            rows = self.db.query(
                """SELECT DISTINCT a.question_id FROM attempts a
                     WHERE a.user_id = ? AND a.is_correct = 0
                     ORDER BY a.created_at DESC""", (user_id,))
            take([r["question_id"] for r in rows])

        if len(chosen) < count:                                                     # 6
            rows = self.db.query(
                "SELECT question_id FROM revision_state WHERE user_id = ? AND due_at <= ?"
                " ORDER BY due_at", (user_id, now_iso()))
            take([r["question_id"] for r in rows])

        if len(chosen) < count:                                                     # 7
            # Full-section coverage: the neighbourhood around whatever is weak,
            # so a session is not a drill on one item.
            neighbourhood: list[str] = []
            for cid in by_colour[RED] + by_colour[ORANGE]:
                for n in self.concepts.neighbours(cid):
                    if n["id"] not in neighbourhood:
                        neighbourhood.append(n["id"])
            take(self._questions_for_concepts(user_id, neighbourhood, seen))

        if len(chosen) < count:                                                     # 8
            rows = self.db.query(
                """SELECT q.id FROM questions q
                     JOIN notebooks n ON n.id = q.primary_notebook_id AND n.owner_id = ?
                    WHERE NOT EXISTS (SELECT 1 FROM attempts a
                                       WHERE a.question_id = q.id AND a.user_id = ?)
                    ORDER BY q.generated_at DESC""", (user_id, user_id))
            take([r["id"] for r in rows])

        return chosen

    # ---------- sessions ----------

    def start_session(self, user_id: str, *, count: int = 20,
                      strategy: str = "adaptive") -> dict:
        """
        The served set is recorded on the session, so the exact questions a
        learner saw are reproducible afterwards -- otherwise a session's
        analysis cannot be re-derived and the record is anecdote.
        """
        question_ids = self.select_questions(user_id, count=count, strategy=strategy)
        if not question_ids:
            raise ValueError(
                "no questions are available to build a session -- generate some first")

        recommended = self.dashboard(user_id)["recommended_question_count"]
        sid = new_id("ses")
        self.db.execute(
            "INSERT INTO revision_sessions (id, user_id, start_time,"
            " recommended_question_count, selected_question_count, selection_strategy,"
            " selected_question_ids_json, completion_status)"
            " VALUES (?,?,?,?,?,?,?, 'in_progress')",
            (sid, user_id, now_iso(), recommended, len(question_ids), strategy,
             json.dumps(question_ids)))
        return {"session_id": sid, "question_ids": question_ids,
                "recommended_question_count": recommended,
                "selected_question_count": len(question_ids), "strategy": strategy}

    def next_question(self, user_id: str, session_id: str) -> dict | None:
        row = self.db.query_one(
            "SELECT * FROM revision_sessions WHERE id = ? AND user_id = ?", (session_id, user_id))
        if row is None:
            raise ValueError("no such session")
        answered = {r["question_id"] for r in self.db.query(
            "SELECT question_id FROM attempts WHERE session_id = ?", (session_id,))}
        for qid in json.loads(row["selected_question_ids_json"]):
            if qid not in answered:
                q = self.db.query_one("SELECT * FROM questions WHERE id = ?", (qid,))
                if q is None:
                    continue
                # The key is deliberately not returned. The reveal happens only
                # after an attempt is recorded.
                return {"question_id": q["id"], "stem": q["stem"],
                        "options": json.loads(q["options_json"]),
                        "family": q["family"], "difficulty": q["difficulty"]}
        return None

    def complete_session(self, user_id: str, session_id: str) -> dict:
        row = self.db.query_one(
            "SELECT * FROM revision_sessions WHERE id = ? AND user_id = ?", (session_id, user_id))
        if row is None:
            raise ValueError("no such session")
        self.db.execute(
            "UPDATE revision_sessions SET end_time = ?, completion_status = 'completed'"
            " WHERE id = ?", (now_iso(), session_id))

        attempts = self.db.query(
            "SELECT * FROM attempts WHERE session_id = ? ORDER BY created_at", (session_id,))
        correct = sum(1 for a in attempts if a["is_correct"])
        colours = {RED: 0, ORANGE: 0, GREEN: 0}
        for a in attempts:
            colours[a["user_colour"]] += 1

        touched: set[str] = set()
        for a in attempts:
            touched.update(json.loads(a["concepts_tested_json"]))

        weak = []
        for cid in touched:
            state = self.knowledge.concept_state(user_id, cid)
            if state and state["colour"] in (RED, ORANGE):
                concept = self.concepts.get(cid)
                weak.append({"concept_id": cid, "colour": state["colour"],
                             "name": concept["canonical_name"] if concept else cid})

        return {
            "session_id": session_id,
            "questions": len(attempts), "correct": correct,
            "incorrect": len(attempts) - correct,
            "colours": colours,
            "weak_concepts": weak,
            "unresolved_gaps": self.knowledge.gaps(user_id),
            "read_list": self.read_list(user_id, session_id),
        }

    def read_list(self, user_id: str, session_id: str) -> list[dict]:
        """
        The exact passages to revise -- never "revise anaemia".

        Built from the gaps this session produced, each pointing at the source
        passage that taught the thing the learner missed.
        """
        rows = self.db.query(
            """SELECT DISTINCT g.id, g.label, g.colour, l.source_id, l.chunk_id,
                      ch.text AS passage, ch.locator_json, n.title AS notebook_title
                 FROM attempts a
                 JOIN gap_links l ON l.attempt_id = a.id
                 JOIN knowledge_gaps g ON g.id = l.gap_id
                 LEFT JOIN source_chunks ch ON ch.id = l.chunk_id
                 LEFT JOIN notebooks n ON n.id = l.notebook_id
                WHERE a.session_id = ? AND a.user_id = ? AND g.resolved_at IS NULL
                ORDER BY CASE g.colour WHEN 'RED' THEN 0 WHEN 'ORANGE' THEN 1 ELSE 2 END""",
            (session_id, user_id))
        out = []
        for i, r in enumerate(rows, start=1):
            item = dict(r)
            item["order"] = i
            item["locator"] = json.loads(r["locator_json"]) if r["locator_json"] else {}
            out.append(item)
        return out
