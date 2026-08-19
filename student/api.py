"""
The learner HTTP API.

Framework-agnostic core, like `benchmark/analytics_api.py`: `handle()` takes a
method, path, params and body and returns `(status, json)`. The transport lives
in `student/server.py`, so this is unit-testable without opening a socket.

Every route except `/auth/*` requires a bearer token and is scoped to that
token's user. Scoping is done in SQL (`WHERE owner_id = ?`), not by filtering
after the fact -- a query that fetches another learner's rows and then discards
them is one refactor away from not discarding them.
"""

from __future__ import annotations

import json
from typing import Any

from .db import Database, new_id, now_iso


class ApiError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.payload = {"error": message, **extra}


class StudentAPI:
    def __init__(self, db: Database, *, engine: Any = None, ai: Any = None,
                 generator: Any = None, validator: Any = None, notifier: Any = None,
                 transparency: Any = None):
        self.db = db
        # The ingestion engine and the AI services are injected so the API can
        # be tested without a provider, and so a deployment can run the API
        # without an AI worker attached.
        self.engine = engine
        self.ai = ai
        self.generator = generator
        self.validator = validator
        self._notifier = notifier
        # The AI-transparency surface. Absent on an install with no benchmark
        # archive, in which case /ai/* answers with what is missing rather
        # than with sample figures -- see student/transparency.py.
        self._transparency = transparency
        self._eval = None

        from .concepts import ConceptStore
        from .knowledge import KnowledgeStore
        from .revision import PriorityEngine, RevisionEngine
        self.concepts = ConceptStore(db)
        self.knowledge = KnowledgeStore(db)
        self.priority = PriorityEngine(db)
        self.revision = RevisionEngine(db)

    def _eval_bundle(self) -> dict | None:
        """
        Compose `benchmark.eval_api.EvalAPI`'s bundle, if this install has an
        archive to compose it from. Composed rather than reimplemented: two
        implementations of the same payload drift, and the one the learner
        sees would be the one nobody noticed had drifted.
        """
        archive = getattr(self.ai, "archive", None)
        if archive is None:
            return None
        if self._eval is None:
            from benchmark.eval_api import EvalAPI
            self._eval = EvalAPI(archive, registry=getattr(self.ai, "registry", None))
        return self._eval.bundle()

    @property
    def transparency(self):
        if self._transparency is None:
            from .transparency import TransparencyService
            archive = getattr(self.ai, "archive", None)
            registry = getattr(self.ai, "registry", None)
            self._transparency = TransparencyService(
                archive=archive, registry=registry, ai_engine=self.ai)
        return self._transparency

    @property
    def notifier(self):
        if self._notifier is None:
            from .notifications import NotificationService
            self._notifier = NotificationService(self.db)
        return self._notifier

    # ---------- request plumbing ----------

    def handle(self, method: str, path: str, params: dict, body: dict | None,
               token: str | None) -> tuple[int, Any]:
        try:
            return self._route(method, path, params, body or {}, token)
        except ApiError as exc:
            return exc.status, exc.payload
        except Exception as exc:  # never leak a stack trace to a client
            return 500, {"error": f"{type(exc).__name__}: {exc}"}

    def _user(self, token: str | None):
        row = self.db.user_for_token(token)
        if row is None:
            raise ApiError(401, "authentication required")
        return row

    def _route(self, method: str, path: str, params: dict, body: dict,
               token: str | None) -> tuple[int, Any]:
        seg = [p for p in path.strip("/").split("/") if p]

        # --- auth (unauthenticated) ---
        if seg[:1] == ["auth"]:
            return self._auth(method, seg[1:], body, token)

        user = self._user(token)
        uid = user["id"]

        if seg == ["me"]:
            return 200, {"id": uid, "email": user["email"], "name": user["name"],
                         "role": user["role"], "timezone": user["timezone"]}

        if seg == ["notebooks"]:
            if method == "GET":
                return 200, {"notebooks": self.list_notebooks(uid)}
            if method == "POST":
                return 201, self.create_notebook(uid, body)

        if len(seg) == 2 and seg[0] == "notebooks":
            if method == "GET":
                return 200, self.get_notebook(uid, seg[1])

        if len(seg) == 3 and seg[0] == "notebooks" and seg[2] == "sources":
            if method == "POST":
                return 202, self.add_source(uid, seg[1], body)

        if len(seg) == 3 and seg[0] == "sources" and seg[2] == "progress":
            if method == "GET":
                return 200, self.source_progress(uid, seg[1])

        # --- questions ---
        if len(seg) == 3 and seg[0] == "notebooks" and seg[2] == "questions":
            if method == "POST":
                return 201, self.generate_questions(uid, seg[1], body)
            if method == "GET":
                return 200, {"questions": self.question_bank(uid, notebook_id=seg[1],
                                                             status=params.get("status"))}

        if seg == ["questions"] and method == "GET":
            return 200, {"questions": self.question_bank(
                uid, concept_id=params.get("concept"), status=params.get("status"))}

        if len(seg) == 2 and seg[0] == "questions" and method == "GET":
            return 200, self.get_question(uid, seg[1])

        if seg == ["demos"]:
            if method == "GET":
                return 200, {"demos": self.list_demos(uid)}
            if method == "POST":
                return 201, self.create_demo(uid, body)

        # --- concepts and the graph ---
        if seg == ["concepts"] and method == "GET":
            return 200, {"concepts": self.priority.ranked(uid)}

        if len(seg) == 2 and seg[0] == "concepts" and method == "GET":
            return 200, self.concept_detail(uid, seg[1])

        if len(seg) == 3 and seg[0] == "concepts" and seg[2] == "graph" and method == "GET":
            return 200, self.concepts.graph_for_user(uid)

        if seg == ["graph"] and method == "GET":
            return 200, self.concepts.graph_for_user(uid, depth_notebook=params.get("notebook"))

        # --- gaps ---
        if seg == ["gaps"] and method == "GET":
            return 200, {"gaps": self.knowledge.gaps(
                uid, colour=params.get("colour"),
                include_resolved=params.get("include_resolved") in ("1", "true"))}

        if len(seg) == 2 and seg[0] == "gaps" and method == "GET":
            return 200, {"gap_id": seg[1],
                         "evidence": self.knowledge.gap_evidence(uid, seg[1])}

        if len(seg) == 3 and seg[0] == "gaps" and seg[2] == "questions" and method == "GET":
            return 200, {"questions": self.gap_questions(uid, seg[1])}

        if len(seg) == 3 and seg[0] == "gaps" and seg[2] == "resolve" and method == "POST":
            self.knowledge.resolve_gap(uid, seg[1])
            return 200, {"ok": True}

        # --- revision ---
        if seg == ["revision", "dashboard"] and method == "GET":
            return 200, self.revision.dashboard(uid)

        if seg == ["revision", "sessions"] and method == "POST":
            return 201, self.start_session(uid, body)

        if seg == ["revision", "next"] and method == "GET":
            session = params.get("session")
            if not session:
                raise ApiError(400, "a session id is required")
            question = self.revision.next_question(uid, session)
            return 200, {"question": question, "finished": question is None}

        if len(seg) == 3 and seg[0] == "revision" and seg[1] == "sessions" \
                and seg[2] == "complete":
            raise ApiError(400, "complete requires a session id: /revision/sessions/<id>/complete")

        if len(seg) == 4 and seg[:2] == ["revision", "sessions"] and seg[3] == "complete" \
                and method == "POST":
            try:
                return 200, self.revision.complete_session(uid, seg[2])
            except ValueError as exc:
                raise ApiError(404, str(exc))

        if seg == ["attempts"] and method == "POST":
            return 201, self.record_attempt(uid, body)

        # --- progress ---
        if seg == ["progress"] and method == "GET":
            return 200, self.progress(uid)

        # --- notifications ---
        if seg == ["settings", "notifications"]:
            if method == "GET":
                return 200, self.notifier.get_prefs(uid)
            if method == "PUT":
                from .notifications import NotificationError
                try:
                    return 200, self.notifier.set_prefs(
                        uid, trigger_time=body.get("trigger_time"), tz=body.get("timezone"),
                        push=body.get("push_enabled"), email=body.get("email_enabled"),
                        note=body.get("note_text"))
                except NotificationError as exc:
                    raise ApiError(400, str(exc))

        if seg == ["settings", "notifications", "test"] and method == "POST":
            return 200, self.notifier.fire(uid)

        if seg == ["settings", "notifications", "history"] and method == "GET":
            return 200, {"history": self.notifier.history(uid)}

        # --- AI transparency (the Quintek AI Benchmark screen) ---
        # Behind authentication like everything else here, but deliberately
        # requires nothing beyond being a learner: the whole point is that a
        # user can check the system that is marking them.
        if seg and seg[0] == "ai":
            return self._ai(method, seg[1:], params)

        raise ApiError(404, f"no such endpoint: {method} /{'/'.join(seg)}")

    def _ai(self, method: str, seg: list[str], params: dict) -> tuple[int, dict]:
        if method != "GET":
            raise ApiError(405, "the AI benchmark screen is read-only")

        service = self.transparency

        if seg == ["benchmark"]:
            return 200, service.overview()

        if seg == ["benchmark", "categories"]:
            return 200, service.categories()

        if seg == ["benchmark", "ranking"]:
            category = params.get("category", "overall")
            try:
                return 200, service.ranking(category)
            except KeyError:
                raise ApiError(404, f"no such benchmark category: {category!r}")

        if seg == ["benchmark", "powering"]:
            return 200, service.powering()

        if seg == ["eval"]:
            # The bundle the existing design files consume, served from the
            # learner backend so the app talks to one origin. A phone should
            # not have to reach the admin console's server to render the
            # screen that tells its owner which AI is marking them.
            bundle = self._eval_bundle()
            if bundle is None:
                raise ApiError(503, "this install has no benchmark archive, so there are no "
                                    "evaluation results to serve")
            return 200, bundle

        if len(seg) == 2 and seg[0] == "models":
            try:
                return 200, service.profile(seg[1])
            except KeyError:
                raise ApiError(404, f"no benchmark run found for model {seg[1]!r}")

        if len(seg) == 3 and seg[0] == "models" and seg[2] == "history":
            history = service.history(seg[1])
            # A model with no runs is a model this archive does not know
            # about. 404 rather than an empty chart, so a mistyped id does not
            # render as "evaluated, no data".
            if not history["points"]:
                raise ApiError(404, f"no benchmark run found for model {seg[1]!r}")
            return 200, history

        raise ApiError(404, f"no such endpoint: {method} /ai/{'/'.join(seg)}")

    def _auth(self, method: str, seg: list[str], body: dict,
              token: str | None) -> tuple[int, Any]:
        if seg == ["register"] and method == "POST":
            email, password = body.get("email", ""), body.get("password", "")
            try:
                uid = self.db.create_user(email, password, name=body.get("name", ""),
                                          tz=body.get("timezone", "UTC"))
            except ValueError as exc:
                raise ApiError(400, str(exc))
            except Exception:
                # UNIQUE(email). Deliberately the same message a duplicate
                # would get from any other path -- registration should not
                # confirm which addresses already exist.
                raise ApiError(409, "that email address cannot be registered")
            return 201, {"user_id": uid, "token": self.db.issue_token(uid)}

        if seg == ["login"] and method == "POST":
            uid = self.db.verify_password(body.get("email", ""), body.get("password", ""))
            if uid is None:
                raise ApiError(401, "email or password is incorrect")
            return 200, {"user_id": uid, "token": self.db.issue_token(uid)}

        if seg == ["logout"] and method == "POST":
            if token:
                self.db.revoke_token(token)
            return 200, {"ok": True}

        raise ApiError(404, f"no such auth endpoint: {method} /auth/{'/'.join(seg)}")

    # ---------- notebooks ----------

    def list_notebooks(self, uid: str) -> list[dict]:
        rows = self.db.query(
            """
            SELECT n.id, n.title, n.subject, n.created_at,
                   (SELECT COUNT(*) FROM sources s WHERE s.notebook_id = n.id) AS source_count,
                   (SELECT COUNT(*) FROM notebook_concepts nc WHERE nc.notebook_id = n.id) AS concept_count,
                   (SELECT COUNT(*) FROM questions q WHERE q.primary_notebook_id = n.id) AS question_count,
                   (SELECT COUNT(*) FROM revision_state rs
                      JOIN questions q2 ON q2.id = rs.question_id
                     WHERE q2.primary_notebook_id = n.id AND rs.user_id = ?
                       AND rs.due_at <= ?) AS due_count
              FROM notebooks n WHERE n.owner_id = ? ORDER BY n.created_at DESC
            """,
            (uid, now_iso(), uid),
        )
        return [dict(r) for r in rows]

    def create_notebook(self, uid: str, body: dict) -> dict:
        title = (body.get("title") or "").strip()
        if not title:
            raise ApiError(400, "a notebook needs a title")
        nid = new_id("nb")
        self.db.execute(
            "INSERT INTO notebooks (id, owner_id, title, subject, created_at) VALUES (?,?,?,?,?)",
            (nid, uid, title, (body.get("subject") or "").strip(), now_iso()),
        )
        return {"id": nid, "title": title, "subject": body.get("subject", ""),
                "source_count": 0, "concept_count": 0, "question_count": 0, "due_count": 0}

    def _owned_notebook(self, uid: str, nid: str):
        row = self.db.query_one("SELECT * FROM notebooks WHERE id = ? AND owner_id = ?", (nid, uid))
        if row is None:
            # 404 rather than 403: whether a notebook exists is itself
            # information about another user's data.
            raise ApiError(404, "no such notebook")
        return row

    def get_notebook(self, uid: str, nid: str) -> dict:
        nb = self._owned_notebook(uid, nid)
        sources = self.db.query(
            "SELECT id, kind, filename, status, page_count, uploaded_at, error"
            "  FROM sources WHERE notebook_id = ? ORDER BY uploaded_at", (nid,))
        concepts = self.db.query(
            "SELECT c.id, c.canonical_name, c.subject, nc.role"
            "  FROM notebook_concepts nc JOIN concepts c ON c.id = nc.concept_id"
            " WHERE nc.notebook_id = ? ORDER BY c.canonical_name", (nid,))
        return {
            "id": nb["id"], "title": nb["title"], "subject": nb["subject"],
            "created_at": nb["created_at"],
            "sources": [dict(r) for r in sources],
            "concepts": [dict(r) for r in concepts],
        }

    # ---------- sources ----------

    def add_source(self, uid: str, nid: str, body: dict) -> dict:
        self._owned_notebook(uid, nid)
        kind = (body.get("kind") or "").strip()
        if kind not in {"pdf", "image", "link", "video", "text", "note"}:
            raise ApiError(400, f"unsupported source kind: {kind!r}")

        text = body.get("text") or ""
        filename = (body.get("filename") or "").strip()
        if kind in {"text", "note"} and not text.strip():
            raise ApiError(400, "a text source needs text")
        if kind == "link" and not (body.get("url") or "").strip():
            raise ApiError(400, "a link source needs a url")

        sid = new_id("src")
        self.db.execute(
            "INSERT INTO sources (id, notebook_id, kind, filename, storage_key,"
            " mime_type, status, uploaded_at) VALUES (?,?,?,?,?,?,?,?)",
            (sid, nid, kind, filename or body.get("url", ""), body.get("storage_key", ""),
             body.get("mime_type", ""), "uploaded", now_iso()),
        )

        if self.engine is None:
            # Honest failure rather than a source stuck at "uploaded" forever
            # with no explanation.
            self.db.execute(
                "UPDATE sources SET status='failed', error=? WHERE id=?",
                ("no ingestion engine is configured on this server", sid))
            raise ApiError(503, "source stored but no ingestion engine is configured")

        self.engine.enqueue_source(sid, raw_text=text, url=body.get("url", ""))
        return {"source_id": sid, "status": "queued"}

    def source_progress(self, uid: str, sid: str) -> dict:
        row = self.db.query_one(
            "SELECT s.*, n.owner_id FROM sources s JOIN notebooks n ON n.id = s.notebook_id"
            " WHERE s.id = ?", (sid,))
        if row is None or row["owner_id"] != uid:
            raise ApiError(404, "no such source")
        chunks = self.db.query(
            "SELECT status, COUNT(*) AS n FROM source_chunks WHERE source_id = ? GROUP BY status",
            (sid,))
        by_status = {r["status"]: r["n"] for r in chunks}
        total = sum(by_status.values())
        done = by_status.get("processed", 0)
        # Chunk failures were counted but never explained, so a source could
        # report "2 failed, 0 processed" with error=null and the only way to
        # learn why was to query the database. The reasons are already stored;
        # this surfaces them.
        chunk_errors = [
            {"ordinal": r["ordinal"], "error": r["error"]}
            for r in self.db.query(
                "SELECT ordinal, error FROM source_chunks WHERE source_id = ?"
                " AND status = 'failed' AND error IS NOT NULL ORDER BY ordinal LIMIT 5", (sid,))]
        concepts = self.db.query_one(
            "SELECT COUNT(DISTINCT concept_id) AS n FROM source_concepts WHERE source_id = ?",
            (sid,))
        return {
            "source_id": sid, "status": row["status"], "error": row["error"],
            "chunks_total": total, "chunks_processed": done,
            "chunks_by_status": by_status,
            "chunks_failed": by_status.get("failed", 0),
            "chunk_errors": chunk_errors,
            "concepts_found": concepts["n"] if concepts else 0,
            # Percent is reported only once the chunk count is known; before
            # that it is null rather than 0, which would read as "no progress"
            # instead of "not yet measurable".
            "percent": round(done / total * 100, 1) if total else None,
        }

    # ---------- questions ----------

    def generate_questions(self, uid: str, notebook_id: str, body: dict) -> dict:
        self._owned_notebook(uid, notebook_id)
        if self.generator is None:
            raise ApiError(503, "no question generator is configured on this server")

        count = int(body.get("count") or 5)
        if not 1 <= count <= 500:
            raise ApiError(400, "count must be between 1 and 500")

        from .generation import GenerationFailed
        try:
            ids = self.generator.generate(
                notebook_id=notebook_id, count=count,
                concept_ids=body.get("concept_ids") or [],
                source_id=body.get("source_id"),
                demo_ids=body.get("demo_ids") or [],
                family=body.get("family", ""), difficulty=body.get("difficulty", ""),
                reasoning_depth=body.get("reasoning_depth", ""),
                constraints=body.get("constraints", ""))
        except GenerationFailed as exc:
            raise ApiError(422, str(exc))
        except Exception as exc:
            raise ApiError(502, f"generation failed: {exc}")

        validation = None
        if self.validator is not None and body.get("validate", True):
            from .validation import ValidationSkipped
            try:
                validation = self.validator.validate_pending(notebook_id=notebook_id,
                                                             limit=len(ids))
            except ValidationSkipped as exc:
                validation = {"skipped": len(ids), "reason": str(exc)}
            except Exception as exc:
                validation = {"error": str(exc)}

        return {"question_ids": ids, "count": len(ids), "validation": validation}

    def question_bank(self, uid: str, *, notebook_id: str | None = None,
                      concept_id: str | None = None, status: str | None = None) -> list[dict]:
        """
        The question repository, deliberately separate from weaknesses and from
        the revision queue -- 'what questions exist for this topic' is a
        different question from 'what should I revise'.
        """
        sql = ["""SELECT q.id, q.stem, q.family, q.difficulty, q.validation_status,
                         q.generated_at, q.primary_notebook_id, n.title AS notebook_title,
                         (SELECT COUNT(*) FROM attempts a
                           WHERE a.question_id = q.id AND a.user_id = ?) AS attempt_count,
                         (SELECT a2.user_colour FROM attempts a2
                           WHERE a2.question_id = q.id AND a2.user_id = ?
                           ORDER BY a2.created_at DESC LIMIT 1) AS last_colour
                    FROM questions q
                    JOIN notebooks n ON n.id = q.primary_notebook_id AND n.owner_id = ?"""]
        params: list = [uid, uid, uid]
        if notebook_id:
            self._owned_notebook(uid, notebook_id)
            sql.append(" AND q.primary_notebook_id = ?")
            params.append(notebook_id)
        if concept_id:
            sql.append(" AND EXISTS (SELECT 1 FROM question_concepts qc"
                       " WHERE qc.question_id = q.id AND qc.concept_id = ?)")
            params.append(concept_id)
        if status:
            sql.append(" AND q.validation_status = ?")
            params.append(status)
        sql.append(" ORDER BY q.generated_at DESC")
        return [dict(r) for r in self.db.query("".join(sql), tuple(params))]

    def get_question(self, uid: str, qid: str) -> dict:
        row = self.db.query_one(
            "SELECT q.*, n.title AS notebook_title FROM questions q"
            " JOIN notebooks n ON n.id = q.primary_notebook_id AND n.owner_id = ?"
            " WHERE q.id = ?", (uid, qid))
        if row is None:
            raise ApiError(404, "no such question")
        q = dict(row)
        q["options"] = json.loads(q.pop("options_json"))
        q["demo_ids"] = json.loads(q.pop("demo_ids_json"))
        q["validation"] = json.loads(q.pop("validation_json") or "{}")
        q["concepts"] = [dict(r) for r in self.db.query(
            "SELECT c.id, c.canonical_name, qc.role FROM question_concepts qc"
            " JOIN concepts c ON c.id = qc.concept_id WHERE qc.question_id = ?", (qid,))]
        if q["chunk_id"]:
            chunk = self.db.query_one(
                "SELECT text, locator_json FROM source_chunks WHERE id = ?", (q["chunk_id"],))
            if chunk:
                q["source_passage"] = chunk["text"]
                q["source_locator"] = json.loads(chunk["locator_json"])
        q["attempts"] = [dict(r) for r in self.db.query(
            "SELECT id, user_answer, is_correct, user_colour, created_at FROM attempts"
            " WHERE question_id = ? AND user_id = ? ORDER BY created_at DESC", (qid, uid))]
        return q

    # ---------- demonstrations ----------

    def list_demos(self, uid: str) -> list[dict]:
        return [dict(r) for r in self.db.query(
            "SELECT * FROM question_demos WHERE owner_id = ? ORDER BY created_at DESC", (uid,))]

    def create_demo(self, uid: str, body: dict) -> dict:
        title = (body.get("title") or "").strip()
        question = (body.get("question") or "").strip()
        if not title or not question:
            raise ApiError(400, "a demonstration needs a title and an example question")
        did = new_id("demo")
        self.db.execute(
            "INSERT INTO question_demos (id, owner_id, title, question, question_type,"
            " difficulty, reasoning_depth, stem_structure, question_target,"
            " distractor_strategy, answer_format, notes, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (did, uid, title, question, body.get("question_type", ""),
             body.get("difficulty", ""), body.get("reasoning_depth", ""),
             body.get("stem_structure", ""), body.get("question_target", ""),
             body.get("distractor_strategy", ""), body.get("answer_format", ""),
             body.get("notes", ""), now_iso()))
        return {"id": did, "title": title}

    # ---------- concepts ----------

    def concept_detail(self, uid: str, concept_id: str) -> dict:
        concept = self.concepts.get(concept_id)
        if concept is None:
            raise ApiError(404, "no such concept")
        ranked = self.priority.explain(uid, concept_id)
        if ranked is None:
            raise ApiError(404, "no such concept")
        state = self.knowledge.concept_state(uid, concept_id) or {}
        return {
            **concept,
            "colour": state.get("colour", "ORANGE"),
            "correct_count": state.get("correct_count", 0),
            "wrong_count": state.get("wrong_count", 0),
            "priority_rank": ranked["rank"],
            "priority_score": ranked["priority_score"],
            "why": ranked["why"],
            "notebooks": self.concepts.notebooks_for(concept_id, uid),
            "related": self.concepts.neighbours(concept_id),
            "gaps": [g for g in self.knowledge.gaps(uid) if g["concept_id"] == concept_id],
            "questions": self.question_bank(uid, concept_id=concept_id),
        }

    # ---------- gaps ----------

    def gap_questions(self, uid: str, gap_id: str) -> list[dict]:
        """'Test me on this' -- questions on the concept behind a gap, unseen
        ones first so the learner retrieves rather than recognises."""
        gap = self.db.query_one(
            "SELECT * FROM knowledge_gaps WHERE id = ? AND user_id = ?", (gap_id, uid))
        if gap is None:
            raise ApiError(404, "no such gap")
        if not gap["concept_id"]:
            linked = self.db.query(
                "SELECT DISTINCT question_id FROM gap_links WHERE gap_id = ?", (gap_id,))
            ids = [r["question_id"] for r in linked if r["question_id"]]
            if not ids:
                return []
            marks = ",".join("?" for _ in ids)
            return [dict(r) for r in self.db.query(
                f"SELECT id, stem, family FROM questions WHERE id IN ({marks})", tuple(ids))]
        return self.question_bank(uid, concept_id=gap["concept_id"])

    # ---------- revision ----------

    def start_session(self, uid: str, body: dict) -> dict:
        count = int(body.get("count") or body.get("selected_question_count") or 20)
        strategy = body.get("strategy") or "adaptive"
        try:
            return self.revision.start_session(uid, count=count, strategy=strategy)
        except ValueError as exc:
            raise ApiError(422, str(exc))

    def record_attempt(self, uid: str, body: dict) -> dict:
        """
        Records the attempt and only then reveals the answer.

        The reveal is the response to this call, never available before it --
        which is what makes 'what you thought' versus 'what was correct' a real
        comparison rather than a formality.
        """
        question_id = body.get("question_id")
        colour = body.get("user_colour") or body.get("colour")
        if not question_id:
            raise ApiError(400, "question_id is required")
        answer = body.get("user_answer")
        try:
            answer = None if answer is None else int(answer)
        except (TypeError, ValueError):
            raise ApiError(400, "user_answer must be an option index")

        try:
            result = self.knowledge.record_attempt(
                user_id=uid, question_id=question_id, user_answer=answer,
                user_colour=colour, session_id=body.get("session_id"),
                gaps=body.get("gaps") or [])
        except ValueError as exc:
            raise ApiError(400, str(exc))

        question = self.db.query_one("SELECT * FROM questions WHERE id = ?", (question_id,))
        reveal = {
            "your_answer": answer,
            "correct_answer": result["correct_answer"],
            "is_correct": result["is_correct"],
            "options": json.loads(question["options_json"]),
            "rationale": question["rationale"],
            "concepts_tested": [dict(r) for r in self.db.query(
                "SELECT c.id, c.canonical_name FROM question_concepts qc"
                " JOIN concepts c ON c.id = qc.concept_id WHERE qc.question_id = ?",
                (question_id,))],
        }
        if question["chunk_id"]:
            chunk = self.db.query_one(
                "SELECT text, locator_json FROM source_chunks WHERE id = ?",
                (question["chunk_id"],))
            if chunk:
                reveal["source_passage"] = chunk["text"]
                reveal["source_locator"] = json.loads(chunk["locator_json"])
        return {"attempt_id": result["attempt_id"], "reveal": reveal}

    # ---------- progress ----------

    def progress(self, uid: str) -> dict:
        counts = self.knowledge.colour_counts(uid)
        total = sum(counts.values())
        attempts = self.db.query_one(
            "SELECT COUNT(*) n, SUM(is_correct) c FROM attempts WHERE user_id = ?", (uid,))
        by_day = self.db.query(
            "SELECT substr(created_at,1,10) d, COUNT(*) n FROM attempts WHERE user_id = ?"
            " GROUP BY d ORDER BY d DESC LIMIT 84", (uid,))
        return {
            "colour_counts": counts,
            "concepts_tracked": total,
            "mastery_pct": round(counts["GREEN"] / total * 100, 1) if total else None,
            "attempts_total": attempts["n"] or 0,
            "attempts_correct": attempts["c"] or 0,
            "accuracy_pct": round((attempts["c"] or 0) / attempts["n"] * 100, 1)
                            if attempts["n"] else None,
            "open_gaps": len(self.knowledge.gaps(uid)),
            "due_count": self.knowledge.due_count(uid),
            "activity": [dict(r) for r in by_day],
        }
