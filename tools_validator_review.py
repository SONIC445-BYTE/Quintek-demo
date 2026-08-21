#!/usr/bin/env python3
"""
Run the two-reviewer protocol over a labelled corpus.

    python3 tools_validator_review.py sheet --reviewer "Dr A" --out review_a.jsonl
        Write a blank review sheet. The corpus labels are NOT included: a
        reviewer shown the answer agrees with it.

    python3 tools_validator_review.py merge --a review_a.jsonl --b review_b.jsonl
        Report agreement, Cohen's kappa, and the items awaiting adjudication.

    python3 tools_validator_review.py merge --a ... --b ... \
            --adjudications adj.jsonl --adjudicator "Dr C"
        The same, with a third reviewer's rulings applied.

    python3 tools_validator_review.py apply --a ... --b ... --out settled.jsonl
        Write the settled labels, refusing while anything is still disputed.

The corpus in this repository is model-authored and every item carries
`label_status: unreviewed`. Running it through this tool with two named
clinicians is what changes that. Nothing else does, and no flag here will.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from validator import review
from validator.devset import load


def _load_adjudications(path):
    if not path:
        return {}, ""
    out = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        raw = json.loads(line)
        out[raw["item_id"]] = review.Judgement(
            item_id=raw["item_id"], label=str(raw["label"]).strip().upper(),
            defect_class=str(raw.get("defect_class") or "").strip(),
            note=str(raw.get("note") or "").strip())
    return out, ""


def cmd_sheet(args):
    devset = load(args.corpus)
    text = review.template(devset.cases, args.reviewer)
    Path(args.out).write_text(text, encoding="utf-8")
    print(f"{len(devset.cases)} item(s) written to {args.out} for {args.reviewer}")
    print("The labels are withheld from this sheet on purpose.")
    return 0


def _merged(args):
    a, b = review.load_sheet(args.a), review.load_sheet(args.b)
    adjudications, _ = _load_adjudications(args.adjudications)
    return review.merge(a, b, adjudications, args.adjudicator)


def cmd_merge(args):
    result = _merged(args)
    print(review.render(result))
    if result["agreement"]["kappa"] is not None and result["agreement"]["kappa"] < 0.6:
        print("\nA kappa below 0.6 means the reviewers are not reliably labelling the same "
              "thing. Fix the labelling instructions before scoring any validator against "
              "this corpus -- the validator's numbers cannot be better than the labels.")
    return 0


def cmd_apply(args):
    result = _merged(args)
    if not result["usable_for_scoring"]:
        print(review.render(result))
        print("\nrefusing to write settled labels while items are disputed or unanswered")
        return 2
    with Path(args.out).open("w", encoding="utf-8") as fh:
        for item_id, row in sorted(result["settled"].items()):
            fh.write(json.dumps({"item_id": item_id, **row}, ensure_ascii=True) + "\n")
    print(f"{len(result['settled'])} settled label(s) written to {args.out}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--corpus", default="corpus/validator_dev")
    sub = parser.add_subparsers(dest="mode", required=True)

    sheet = sub.add_parser("sheet")
    sheet.add_argument("--reviewer", required=True)
    sheet.add_argument("--out", required=True)

    for name in ("merge", "apply"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--a", required=True)
        cmd.add_argument("--b", required=True)
        cmd.add_argument("--adjudications", default="")
        cmd.add_argument("--adjudicator", default="")
        if name == "apply":
            cmd.add_argument("--out", required=True)

    args = parser.parse_args(argv)
    return {"sheet": cmd_sheet, "merge": cmd_merge, "apply": cmd_apply}[args.mode](args)


if __name__ == "__main__":
    raise SystemExit(main())
