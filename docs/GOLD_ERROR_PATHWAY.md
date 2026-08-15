# Gold Error and Appeal Pathway

Gold labels can be wrong. The benchmark must be able to discover and repair them without corrupting historical runs.

## Item lifecycle

`proposed -> independently reviewed -> verified -> released -> challenged -> adjudicated -> corrected/superseded`

## Challenge triggers

- two or more competent reviewers flag the gold
- candidate output is consistently contrary to gold across independent systems
- new authoritative evidence invalidates the gold
- ambiguity is discovered
- answer key or concept mapping is internally inconsistent

## Challenge process

1. Freeze the original item.
2. Open a gold challenge record.
3. Gather provenance/evidence.
4. Assign two independent medical reviewers.
5. If disagreement persists, senior adjudicator decides.
6. Record old and new gold labels.
7. Increment dataset version.
8. Never rewrite historical run results.
9. Re-run affected regression items.

## Gold-error detection signal

A model being correct does NOT automatically prove gold is wrong.

Use:
- independent evidence
- reviewer agreement
- authoritative reference
- cross-model consistency only as supporting evidence

## Reporting

Every corrected gold item gets:
- reason
- reviewer IDs
- adjudicator
- old hash
- new hash
- effective dataset version

Historical benchmark runs remain immutable.
