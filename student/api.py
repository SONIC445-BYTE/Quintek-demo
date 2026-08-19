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
    def __init__(self, db: Database, *, engine: Any = None):
        self.db = db
        # The AI engine (ingestion/generation). Injected so the API can be
        # tested without a provider, and so a deployment can run the API
        # without an AI worker attached.
        self.engine = engine

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

        raise ApiError(404, f"no such endpoint: {method} /{'/'.join(seg)}")

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
        concepts = self.db.query_one(
            "SELECT COUNT(DISTINCT concept_id) AS n FROM source_concepts WHERE source_id = ?",
            (sid,))
        return {
            "source_id": sid, "status": row["status"], "error": row["error"],
            "chunks_total": total, "chunks_processed": done,
            "chunks_by_status": by_status,
            "concepts_found": concepts["n"] if concepts else 0,
            # Percent is reported only once the chunk count is known; before
            # that it is null rather than 0, which would read as "no progress"
            # instead of "not yet measurable".
            "percent": round(done / total * 100, 1) if total else None,
        }
