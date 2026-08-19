"""
The concept graph: global concepts, resolution, relationships, cross-notebook
links.

Concepts are **global**, not per-notebook. There is one `Ferritin`, referenced
from Medicine, Biochemistry and Pathology alike. That is what makes a Medicine
question able to test a Biochemistry concept while staying in the Medicine
notebook, and it is enforced by `UNIQUE(normalized_name)` in the schema rather
than by convention here.

## Why resolution is deliberately timid

Concept resolution is the one step in ingestion that can silently corrupt a
learner's graph. A **false merge** -- collapsing `transferrin` and
`transferrin saturation` into one node -- is close to unrecoverable: every
question, gap and priority signal attached to either concept is now attached to
a concept that does not exist, and no later correction can separate the
evidence again.

The asymmetry matters. A false *split* (two nodes that should be one) is
visible, annoying and fixable with an alias. A false *merge* destroys
information. So this module matches on:

  * exact normalized name, or
  * an explicit alias someone recorded,

and nothing else. There is no fuzzy or embedding-based auto-merge. Near
matches are recorded as *suggestions* for a human, never applied. The benchmark
gates this same behaviour with `GATE-C-MERGE` (false-merge rate, upper bound
0.03) -- this module is the production-side counterpart of that gate.
"""

from __future__ import annotations

import re
import unicodedata

from .db import Database, new_id, now_iso

# Relationship vocabulary from docs/QUINTEK_LOGIC.md section 3. Stored as text
# with this lookup rather than a CHECK constraint, so extending it is a code
# change and not a migration.
RELATION_TYPES = {
    "related_to", "prerequisite_of", "causes", "caused_by", "mechanism_of",
    "manifestation_of", "diagnostic_feature_of", "complication_of",
    "treatment_of", "differential_of", "contrasts_with", "measured_by",
    "associated_with",
}

_PUNCT = re.compile(r"[^\w\s-]", re.UNICODE)
_SPACE = re.compile(r"\s+")


def normalize(name: str) -> str:
    """
    Canonical key for concept identity.

    Case, accents, surrounding punctuation and whitespace runs are noise --
    `Ferritin`, `ferritin` and `Ferritin.` are one concept. Everything else is
    left alone. In particular there is **no stemming or singularisation**:
    "basis"/"bases" and "cell"/"cells" would collapse under a naive stemmer,
    and in medicine that is a false merge, which this module exists to avoid.
    """
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = _PUNCT.sub(" ", text.lower())
    return _SPACE.sub(" ", text).strip()


class ConceptStore:
    """Reads and writes the global concept graph."""

    def __init__(self, db: Database):
        self.db = db

    # ---------- resolution ----------

    def find(self, name: str) -> str | None:
        """Concept id for a name, by exact normalized match or a recorded alias."""
        key = normalize(name)
        if not key:
            return None
        row = self.db.query_one("SELECT id FROM concepts WHERE normalized_name = ?", (key,))
        if row:
            return row["id"]
        row = self.db.query_one("SELECT concept_id FROM concept_aliases WHERE normalized = ?",
                                (key,))
        return row["concept_id"] if row else None

    def resolve_or_create(self, name: str, *, subject: str = "",
                          description: str = "") -> str:
        """
        Return the id for `name`, creating the concept if it is genuinely new.

        Never merges on similarity. Two names that look alike stay separate
        until someone records an alias, because the cost of being wrong is
        asymmetric (see the module docstring).
        """
        name = (name or "").strip()
        key = normalize(name)
        if not key:
            raise ValueError("a concept needs a name")

        existing = self.find(name)
        if existing:
            # Fill in detail a later, richer mention supplies -- but never
            # overwrite a description that is already there.
            if subject or description:
                self.db.execute(
                    "UPDATE concepts SET subject = CASE WHEN subject = '' THEN ? ELSE subject END,"
                    " description = CASE WHEN description = '' THEN ? ELSE description END"
                    " WHERE id = ?", (subject, description, existing))
            return existing

        cid = new_id("cpt")
        self.db.execute(
            "INSERT INTO concepts (id, canonical_name, normalized_name, subject, description,"
            " first_seen_at) VALUES (?,?,?,?,?,?)",
            (cid, name, key, subject, description, now_iso()))
        return cid

    def add_alias(self, concept_id: str, alias: str) -> None:
        """
        Record that `alias` means an existing concept.

        This is the *only* way two differently-spelled names become one node,
        and it is deliberately an explicit act. Aliasing a name that already
        belongs to a different concept is refused rather than silently
        repointed -- that would be a merge by the back door.
        """
        key = normalize(alias)
        if not key:
            return
        owner = self.find(alias)
        if owner == concept_id:
            return
        if owner is not None:
            raise ValueError(
                f"{alias!r} already resolves to concept {owner}; merging two existing "
                "concepts is not an alias operation and must be done deliberately")
        self.db.execute(
            "INSERT INTO concept_aliases (id, concept_id, alias, normalized) VALUES (?,?,?,?)",
            (new_id("als"), concept_id, alias, key))

    def merge_candidates(self, threshold: float = 0.86) -> list[dict]:
        """
        Pairs a human might want to alias together. **Advisory only.**

        Nothing in this module acts on the result. It exists so a reviewer can
        find genuine duplicates ("Vitamin B12" / "vitamin b-12") without the
        system taking the decision itself.
        """
        rows = self.db.query("SELECT id, canonical_name, normalized_name FROM concepts")
        out = []
        for i, a in enumerate(rows):
            for b in rows[i + 1:]:
                score = _similarity(a["normalized_name"], b["normalized_name"])
                if score >= threshold:
                    out.append({"a": a["id"], "a_name": a["canonical_name"],
                                "b": b["id"], "b_name": b["canonical_name"],
                                "similarity": round(score, 3),
                                "action": "review — not merged automatically"})
        return sorted(out, key=lambda r: -r["similarity"])

    # ---------- links ----------

    def link_to_notebook(self, notebook_id: str, concept_id: str, role: str = "primary") -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO notebook_concepts (notebook_id, concept_id, role)"
            " VALUES (?,?,?)", (notebook_id, concept_id, role))

    def link_to_source(self, source_id: str, concept_id: str, chunk_id: str | None) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO source_concepts (source_id, concept_id, chunk_id)"
            " VALUES (?,?,?)", (source_id, concept_id, chunk_id))

    def relate(self, source_concept_id: str, target_concept_id: str, relation_type: str,
               *, confidence: float = 0.0, provenance_source_id: str | None = None) -> None:
        if relation_type not in RELATION_TYPES:
            raise ValueError(f"unknown relation type: {relation_type!r}")
        if source_concept_id == target_concept_id:
            return  # a concept relating to itself carries no information
        self.db.execute(
            "INSERT OR IGNORE INTO concept_relationships (id, source_concept_id,"
            " target_concept_id, relation_type, confidence, provenance_source_id, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (new_id("rel"), source_concept_id, target_concept_id, relation_type,
             confidence, provenance_source_id, now_iso()))

    # ---------- reads ----------

    def get(self, concept_id: str) -> dict | None:
        row = self.db.query_one("SELECT * FROM concepts WHERE id = ?", (concept_id,))
        return dict(row) if row else None

    def neighbours(self, concept_id: str) -> list[dict]:
        rows = self.db.query(
            """
            SELECT r.relation_type, r.confidence, c.id, c.canonical_name, c.subject, 'out' AS dir
              FROM concept_relationships r JOIN concepts c ON c.id = r.target_concept_id
             WHERE r.source_concept_id = ?
            UNION ALL
            SELECT r.relation_type, r.confidence, c.id, c.canonical_name, c.subject, 'in' AS dir
              FROM concept_relationships r JOIN concepts c ON c.id = r.source_concept_id
             WHERE r.target_concept_id = ?
            """, (concept_id, concept_id))
        return [dict(r) for r in rows]

    def notebooks_for(self, concept_id: str, owner_id: str) -> list[dict]:
        rows = self.db.query(
            "SELECT n.id, n.title, n.subject, nc.role FROM notebook_concepts nc"
            " JOIN notebooks n ON n.id = nc.notebook_id"
            " WHERE nc.concept_id = ? AND n.owner_id = ? ORDER BY n.title",
            (concept_id, owner_id))
        return [dict(r) for r in rows]

    def graph_for_user(self, owner_id: str, *, depth_notebook: str | None = None) -> dict:
        """
        Nodes and edges for the concept-graph screen.

        A cross-subject edge is flagged, because those are the ones worth
        drawing differently: they are the evidence that the learner's knowledge
        is connected across notebooks rather than siloed by subject.
        """
        params: tuple = (owner_id,)
        clause = ""
        if depth_notebook:
            clause = " AND nc.notebook_id = ?"
            params = (owner_id, depth_notebook)

        nodes = self.db.query(
            f"""
            SELECT DISTINCT c.id, c.canonical_name, c.subject
              FROM concepts c JOIN notebook_concepts nc ON nc.concept_id = c.id
              JOIN notebooks n ON n.id = nc.notebook_id
             WHERE n.owner_id = ?{clause}
            """, params)
        ids = {n["id"] for n in nodes}
        edges = []
        for row in self.db.query(
                "SELECT source_concept_id, target_concept_id, relation_type, confidence"
                "  FROM concept_relationships"):
            if row["source_concept_id"] in ids and row["target_concept_id"] in ids:
                edges.append(dict(row))

        by_id = {n["id"]: dict(n) for n in nodes}
        for edge in edges:
            a, b = by_id[edge["source_concept_id"]], by_id[edge["target_concept_id"]]
            edge["cross_subject"] = bool(a["subject"] and b["subject"]
                                         and a["subject"] != b["subject"])
        return {"nodes": list(by_id.values()), "edges": edges}


def _similarity(a: str, b: str) -> float:
    """Token-overlap similarity, used only to *suggest* review pairs."""
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)
