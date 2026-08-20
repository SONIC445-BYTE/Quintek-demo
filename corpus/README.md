# Quintek corpus

Three files, three different jobs, three different levels of authority. The
difference between them is the whole point, so it is worth reading before
adding anything.

| File | What it is | Can it score a model? |
|---|---|---|
| `gold.jsonl` | Expert-authored items with references | **Yes** — this is the benchmark |
| `development.jsonl` | Model-authored items, 30 of them | **No** — pipeline exercise only |
| `adversarial.jsonl` | Deliberately broken items, 20 of them | **Yes, for rejection** — see below |

`gold.jsonl` does not exist yet. That is the honest state of the project.

## The rule, and why it is in code

`benchmark/corpus.py` refuses to parse an item whose `provenance` is
`model_authored` and whose `gold_standard` is `true`. There is no override
flag, and a test asserts there is none.

The reason is not pedantry. If a model authors the gold, a benchmark run
scored against it measures agreement between a model and a model, and reports
that as accuracy. The resulting number is indistinguishable from a real one —
it flows through `benchmark/analytics.py`, into the leaderboard, into the
promotion gate, and onto the learner-facing transparency screen that exists
specifically to tell a student how much to trust the AI marking their work.

A corpus is not a pile of questions. It is the thing that decides what
"correct" means, and it borrows all of its authority from whoever wrote it.

## Why the adversarial file is different, and is not a loophole

A positive item asserts *"this is the correct answer."* That claim needs
medical authority.

A negative item asserts *"this question is broken, in this specific way."*
When the item was **constructed** broken, that claim is true by construction.
A question whose key points at option 5 of 4 is defective whoever wrote it. A
stem asserting the opposite of its own source passage is ungrounded whoever
wrote it.

So the adversarial battery is usable right now, and measuring rejection is the
one real evaluation available before the expert corpus exists. It is also the
more interesting property: a system that generates unevenly but reliably
refuses bad output is usable, while one that generates well and approves
everything has no validator at all.

Run it with `python3 tools_adversarial_run.py --provider nvidia --validator <model>`.

## The facet model

Every concept is tested three ways:

- `definition` — what the thing is
- `mechanism` — why it behaves as it does
- `clinical` — what you do about it in front of a patient

A model that answers the definition and fails the mechanism has not understood
the concept, and a corpus of definitions cannot tell. `coverage()` reports per
`(concept, facet)` so the gap is visible rather than assumed away. All 10
concepts in `development.jsonl` carry all three facets.

## Adding items

```jsonc
{
  "id": "gold-001",
  "subject": "PSM", "topic": "Screening", "concept": "Sensitivity",
  "facet": "definition",              // definition | mechanism | clinical
  "question_type": "mcq",             // mcq | vignette | conceptual | assertion_reason | sequence
  "difficulty": "pg_entry",           // foundation | pg_entry | pg_advanced
  "stem": "...",
  "options": ["...", "..."],
  "correct_index": 0,
  "explanation": "why the key is right AND why the distractors are wrong",
  "reference": "Park 26e, Screening chapter, p.142",
  "provenance": "expert_authored",    // see below
  "gold_standard": true,
  "reviewed_by": "", "reviewed_at": ""
}
```

`provenance` values:

- `expert_authored` — a qualified human wrote it against a named reference. May be gold.
- `published_source` — taken from an attributable published source, with permission. May be gold.
- `model_authored` — a model wrote it. **Never** gold; a development-set item.
- `model_authored_expert_reviewed` — model-written, then checked by a named human. May be gold **once `reviewed_by` is filled in**. An unnamed reviewer is not a reviewer.

Every gold item needs a `reference`. An item that cannot be checked against a
source cannot be challenged, and a corpus nobody can challenge is not gold.

## Promoting the development set

The 30 items in `development.jsonl` are a reasonable starting point for expert
review — they cover 10 concepts across all three facets and carry references.
Promoting one means: a qualified person reads it, corrects it or confirms it,
sets `provenance` to `model_authored_expert_reviewed`, fills in `reviewed_by`
and `reviewed_at`, and sets `gold_standard: true`. The loader will then accept
it, and it becomes scorable.

Nothing about this is automatable, which is the point.
