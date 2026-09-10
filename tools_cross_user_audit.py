#!/usr/bin/env python3
"""
Find learner data that ended up in another learner's rows.

Two defects wrote across the ownership boundary before they were fixed:

  * `POST /attempts` accepted any question id, recording an attempt against a
    question the caller did not own (fixed in d3d0506);
  * `POST /notebooks/<id>/questions` accepted `source_id` and `concept_ids`
    unscoped, so generation retrieved another learner's chunks and STORED the
    passage into the caller's own question row -- which then passed every
    later ownership check, because the question genuinely is the caller's.

The second is why this tool exists. Fixing the code stops new rows being
written; it does nothing about rows already written. Those rows look correct
to every query in the application.

THIS TOOL DELETES NOTHING. It reports. What to do about a finding is a
decision about someone's data, and it is not one a script should take.

    python3 tools_cross_user_audit.py path/to/quintek.db
    python3 tools_cross_user_audit.py path/to/quintek.db --json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys

# A question whose grounding chunk belongs to a source owned by someone other
# than the owner of the notebook the question was generated into.
LEAKED_QUESTIONS = """
SELECT q.id                AS question_id,
       q.primary_notebook_id,
       nq.owner_id         AS question_owner,
       q.source_id,
       q.chunk_id,
       ns.owner_id         AS source_owner,
       q.generated_at,
       substr(q.stem, 1, 80) AS stem_head
  FROM questions q
  JOIN notebooks nq ON nq.id = q.primary_notebook_id
  JOIN sources   s  ON s.id  = q.source_id
  JOIN notebooks ns ON ns.id = s.notebook_id
 WHERE nq.owner_id <> ns.owner_id
 ORDER BY q.generated_at
"""

# An attempt recorded by one learner against another learner's question.
LEAKED_ATTEMPTS = """
SELECT a.id        AS attempt_id,
       a.user_id   AS attempt_user,
       a.question_id,
       nq.owner_id AS question_owner,
       a.created_at
  FROM attempts a
  JOIN questions q  ON q.id  = a.question_id
  JOIN notebooks nq ON nq.id = q.primary_notebook_id
 WHERE a.user_id <> nq.owner_id
 ORDER BY a.created_at
"""

# Whose text is sitting in whose notebook, as a summary a human can act on.
EXPOSURE_PAIRS = """
SELECT ns.owner_id AS source_owner,
       nq.owner_id AS question_owner,
       COUNT(*)    AS questions,
       COUNT(DISTINCT q.source_id) AS sources_involved
  FROM questions q
  JOIN notebooks nq ON nq.id = q.primary_notebook_id
  JOIN sources   s  ON s.id  = q.source_id
  JOIN notebooks ns ON ns.id = s.notebook_id
 WHERE nq.owner_id <> ns.owner_id
 GROUP BY ns.owner_id, nq.owner_id
 ORDER BY questions DESC
"""


def audit(path: str) -> dict:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return {
            "database": path,
            "leaked_questions": [dict(r) for r in conn.execute(LEAKED_QUESTIONS)],
            "leaked_attempts": [dict(r) for r in conn.execute(LEAKED_ATTEMPTS)],
            "exposure_pairs": [dict(r) for r in conn.execute(EXPOSURE_PAIRS)],
            "totals": {
                "questions": conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0],
                "attempts": conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0],
                "users": conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
            },
        }
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("database")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    report = audit(args.database)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 1 if (report["leaked_questions"] or report["leaked_attempts"]) else 0

    t = report["totals"]
    print(f"{args.database}: {t['users']} user(s), {t['questions']} question(s), "
          f"{t['attempts']} attempt(s)\n")

    q, a = report["leaked_questions"], report["leaked_attempts"]
    if not q and not a:
        print("No cross-user rows found.")
        print("Note: this is evidence about THIS database only. A database that never "
              "had two users cannot show the defect.")
        return 0

    if q:
        print(f"QUESTIONS GROUNDED IN ANOTHER LEARNER'S SOURCE: {len(q)}")
        print("  Each row holds that learner's passage text in `source_passage`,"
              " served by GET /questions/<id>.")
        for r in q[:20]:
            print(f"   {r['question_id']}  owner={r['question_owner']}"
                  f"  <- source {r['source_id']} owned by {r['source_owner']}"
                  f"  [{r['generated_at']}]")
        if len(q) > 20:
            print(f"   ... and {len(q) - 20} more (use --json for all)")
        print()
        print("  BY PAIR:")
        for r in report["exposure_pairs"]:
            print(f"   {r['source_owner']}'s material -> {r['question_owner']}'s notebooks:"
                  f" {r['questions']} question(s) from {r['sources_involved']} source(s)")
        print()

    if a:
        print(f"ATTEMPTS ON ANOTHER LEARNER'S QUESTION: {len(a)}")
        for r in a[:20]:
            print(f"   {r['attempt_id']}  by {r['attempt_user']}"
                  f"  on {r['question_id']} owned by {r['question_owner']}"
                  f"  [{r['created_at']}]")
        if len(a) > 20:
            print(f"   ... and {len(a) - 20} more (use --json for all)")
        print()

    print("Nothing has been changed. See docs/CROSS_USER_REMEDIATION.md before deciding.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
