# Cross-user data written before the ownership fixes

Status: **detection built, nothing remediated.** Deleting or altering learner
data is a decision for the person who owns the deployment, so this document
proposes and does not act.

Run `python3 tools_cross_user_audit.py <database>` to get the counts for a real
database. It opens read-only and changes nothing.

## What was written, and by which defect

| Defect | Fixed in | What it wrote |
|---|---|---|
| `POST /attempts` took any question id | `d3d0506` | An `attempts` row by one learner against another learner's question |
| `generate_questions` passed `source_id` unscoped | this change | A `questions` row in the caller's notebook, grounded in another learner's source |
| `generate_questions` passed `concept_ids` unscoped | this change | The same, and **without needing a borrowed id** — concepts are a global vocabulary, so any learner studying a shared topic retrieved every other learner's chunks on it |

The third is the one that matters for volume. It needed no attack and no
guessed identifier: it is what the ordinary "make questions about this concept"
flow did whenever two learners studied the same topic.

## Why these rows are invisible to the application

A leaked question row is **genuinely owned by the learner who generated it**.
`primary_notebook_id` points at their notebook; they created it; every
ownership check in the codebase passes, correctly. The row is not mislabelled —
what is wrong is the material it was built from. No query in the application
distinguishes it, which is why the detector exists as a separate tool.

## The distinction that decides remediation

**There is no passage column on `questions`.** The stored row holds only
`source_id` and `chunk_id`. `GET /questions/<id>` and the attempt reveal fetch
the passage by joining live to `source_chunks` at read time.

That splits the exposure in two, and the halves need different answers:

**Direct disclosure — the verbatim passage.** Served by a live join. Severing
`chunk_id` and `source_id` on the affected rows stops it immediately and
completely. Nothing needs deleting for this half.

**Derived disclosure — the stem, options and rationale.** These were written by
a model that had the other learner's passage in its prompt. They are stored
text in the row itself. Severing the link does not touch them, and for medical
source material the derived text is where the specifics live: a stem built from
a confidential ward note can carry the patient's values even with no passage
attached. **Do not assume severing the link is sufficient.** It is sufficient
for the passage and not for what was written from it.

## Options

1. **Quarantine (reversible, recommended as the first move).** Add
   `quarantined_at` to `questions`, set it on the affected rows, exclude them
   from every read. Nothing is destroyed, the audit trail survives, and the
   decision stays open. Costs one additive migration and a `WHERE` clause on
   the question reads.

2. **Sever the grounding.** `UPDATE questions SET source_id='', chunk_id=''`
   on affected rows. Stops the verbatim half only. Cheap, irreversible in
   the sense that provenance is lost, and leaves the derived text in place —
   so it is the *weakest* option, not the moderate one it looks like.

3. **Delete the affected question rows.** Removes both halves. Also removes
   attempt history and the concept-state those attempts produced, for a learner
   who did nothing wrong and believes those questions are theirs. Needs a plan
   for the dependent `attempts`, `question_concepts` and `gap_links` rows.

4. **Delete the cross-user `attempts` rows** (the `d3d0506` defect). Lower
   stakes: these are attempts a learner made against a question that was never
   theirs, and removing them corrects that learner's own statistics rather than
   taking anything from them. `_refresh_concept_state` would need rerunning for
   the affected concepts.

My reading: **1 then decide, not 2.** Option 2 is the tempting middle and it
buys less than it appears to, because it addresses the half that a live join
already makes easy to stop and ignores the half that is actually stored.

## Notification

Whether the owners of the *source* material need telling is a question about
your obligations, not about the code. What the tool can tell you is exactly
which pairs of learners are involved and how many rows — that is the
`BY PAIR` section of its output.

## What cannot be established from the repository

No production database exists in the development container; `quintek.db` is
gitignored and the checkout is clean. The counts are unknown until the tool is
run against a real deployment. A deployment that has only ever had one user
cannot have the defect at all — the joins compare two owners.
