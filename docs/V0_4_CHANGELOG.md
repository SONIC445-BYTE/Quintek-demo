# v0.4 Changelog — Defects Found in v0.3 and How They Were Fixed

v0.3 was a substantial improvement over v0.2 and closed eight of the nine gaps raised in review.
The defects below are what remained. Several are notable less for their size than for the fact that
v0.3 explicitly claimed to have fixed them.

## 1. Artifacts that the changelog claimed were removed, and a test that could not see them

v0.3's changelog stated: *"Artifact hygiene: Known generated-text artifacts are removed."* They were
not. Two remained, in `DECISION_LOG.md` and `CONTAMINATION_PROTOCOL.md`.

The failure mode is worth recording. v0.3 shipped a test:

```python
assert "cite" + "turn0search1" not in s   # literal split so this doc passes its own scan
```

which passed. It passed because the actual bytes contain Unicode Private Use Area characters
between `cite` and `turn` (`U+E200` and neighbours), so the literal string never matched. A green
test sat two files away from the defect it existed to catch — in a package whose subject is
detecting exactly this class of false assurance.

**Fixed:** artifacts removed with a PUA-tolerant regex; all files swept for stray PUA characters;
`test_no_citation_artifacts_anywhere` and `test_no_private_use_area_characters` now match the real
pattern. The MedGemma contamination claim is flagged `CITATION REQUIRED` — it travelled through
three spec versions on a broken citation token and was never verified by a human.

## 2. Thresholds duplicated in four places, drifting apart

Minimum sample sizes appeared in `SAMPLE_SIZE_AND_STATISTICS.md`, `gate_registry_v0_3.json`,
`v0_3.yaml`, and `TRACK_GATES.md`, and disagreed:

| Quantity | Prose doc | Registry |
|---|---|---|
| Medical QA | 400 | 500 |
| Relationship extraction | 400 | 500 |
| Fake mastery | 100 concepts x ≥6 questions | 300, unit unstated |
| Near-duplicate failure | >20% | ≤8% |

The near-duplicate pair differ by 2.5x. An implementer had no way to determine which was
authoritative.

**Fixed:** the gate registry is now the sole source of truth. Prose documents reference gate IDs and
state no numbers. `test_registry_is_sole_source_of_thresholds` fails the build on any restatement —
it caught two violations in documents written during this revision, which is the intended behaviour.
Units (`n_unit`) are now mandatory per gate, because "300" meant items in one place and concepts in
another.

## 3. Gates that were specified but unenforceable

`GATE_DERIVATION.md` v0.3 set a concept false-merge threshold of 0.03. No such gate existed in the
registry or `TRACK_GATES.md`. The most dangerous ontology failure available to this system —
silently collapsing two clinically distinct concepts — was thresholded, discussed, and could not
fail a run. Macro F1 stays high while false merges concentrate in exactly the clinically adjacent
pairs where merging is most harmful.

Family coverage had the same shape: defined as a per-concept condition, never aggregated to a
run-level gate, so a candidate could fail coverage on most concepts and still pass.

**Fixed:** `GATE-C-MERGE` and `GATE-H-FAMILY` added. `test_all_mandatory_gates_present` pins the
full set.

## 4. A safety gate that could not be evaluated

v0.3 defined the CME gate as `direction: equal, threshold: 0, ci: exact_binomial` with no maximum
tolerated upper bound. That is not implementable as a pass/fail rule: zero observed events is
compatible with a wide range of true rates, so `0/50` and `0/500` both read as PASS while differing
by an order of magnitude in evidential strength. `min_n` was decorative.

**Fixed:** the gate now requires zero confirmed adjudicated events **and** a Clopper-Pearson
one-sided 95% upper bound at or below `max_tolerated_upper_bound`. Verified arithmetic:

| n | zero-event 95% upper bound | clears 0.01? |
|---:|---:|---|
| 200 | 0.0149 | no |
| 300 | 0.0099 | marginal |
| 500 | 0.0060 | yes |

`test_safety_min_n_actually_achieves_its_upper_bound` recomputes this, so a future edit that lowers
`min_n` without raising the bound fails the build.

## 5. Three severity scales, silently conflated

v0.3 carried item severity (`low..critical`), CME categories (`CME-1..6`), and product-harm tiers
(`H0..H3`) without ever mapping them. `TRACK_GATES.md` then wrote `Confirmed H3/Critical Medical
Error`, equating a harm tier with a failure category. They index different objects: severity is a
property of the question, a CME is a property of the answer, a harm tier is a property of the
outcome. The conflation permits two opposite readings of the same run.

**Fixed:** `docs/SEVERITY_TAXONOMY.md` maps all three and defines a gate event as a four-clause
conjunction. Adds `H3-OMISSION` for harm arising from omission rather than false statement — a case
that fits no CME category cleanly and was previously invisible. Adds distribution constraints on the
safety holdout, because a corpus of uniformly similar high-severity items satisfies `min_n` while
measuring almost nothing.

## 6. Integrity rendered as a score

**Adopted from your architectural proposal, with one change.** A Benchmark Integrity Score displayed
alongside performance still anchors the reader on the performance number; `94.2% PASS` next to
`contamination: compromised` is remembered as 94.2%.

Integrity is not a dimension of quality. It is a condition of measurement. A contaminated run is not
a worse measurement — it is not a measurement.

**Fixed:** `docs/SCORECARD_SPEC.md` makes integrity a precondition that **suppresses** performance
output entirely. `report.json["scores"]` is `null` on an invalid run, so a consumer reading
`report["scores"]["A_medical_qa"]` raises rather than silently retrieving a number from a run whose
controls failed. Numeric integrity scores and aggregate overall scores are both prohibited.

## 7. A budget that could not fund the benchmark's own purpose

v0.3 set 25,000 calls without stating whether that was per-candidate or global. Measured against its
own registered sample sizes, one full run costs ~9,200 calls — capping a bake-off at two candidates,
in a benchmark built to compare candidates.

**Fixed:** per-candidate and global budgets declared separately; preflight cost projection required;
`test_budget_covers_one_full_run` verifies the ceiling against the registry.

## 8. The human cost was never stated

Every version specified reviewer *standards* with increasing precision and never once specified
reviewer *quantity*.

**Added:** `docs/REVIEW_CAPACITY.md`. First full qualification cycle costs roughly 1,200–1,700 hours
of qualified medical expert time — ~800–1,200 for corpus authoring, ~400–500 for review and
adjudication. Establishes that a single reviewer cannot satisfy the spec as a matter of arithmetic
rather than policy: Cohen's kappa is undefined with one rater, `GATE-REL-KAPPA-CRITICAL` is
mandatory, therefore no PASS is reachable. Defines three honest operating modes and requires
`max_attainable_outcome` to be computed and printed *before* a run consumes budget.

## 9. The build prompt was still v0.2

`MASTER_VIBE_CODING_PROMPT_V0_2.md` shipped unchanged inside the v0.3 package and referenced none of
the v0.3 documents. An agent handed that file would have built the v0.2 architecture and reported
success.

**Fixed:** replaced by `docs/MASTER_BUILD_PROMPT_V0_4.md`. Added `docs/INDEX.md` with an explicit
authority hierarchy so conflicts resolve deterministically.

## What did not change

The core architecture is sound and was left alone: source/benchmark separation, the no-self-grading
rule, judge tiers, the gold challenge lifecycle, contamination protocol, injection battery, variance
protocol, candidate identity definition, and the all-candidates-fail policy. v0.3 got these right.
