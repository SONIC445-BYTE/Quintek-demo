"""
The clean-label audit is a worksheet, not a verdict.

WHY THE STRONGEST TESTS HERE ARE ABOUT WHAT IT DOES NOT DO
------------------------------------------------------------
A real run flagged `vd-clean-016` as a false positive, and reading it, the
check was right: the passage defines the apparent volume of distribution as
the amount of drug IN THE BODY over plasma concentration, and the explanation
attributes "the ratio of DOSE to plasma concentration" to that same passage.
Different quantities. So a clean label was wrong, and the whole corpus is
`label_status: unreviewed`, `gold_standard: false`, `provenance:
model_authored`, zero reviewers.

The tempting response is to relabel the item. That would be tuning the ground
truth to the result that embarrassed it, by the party being measured. The tool
therefore produces a list for a clinician and changes nothing, and the tests
below hold it to that -- because the failure mode is silent and the corpus on
disk is the only record anyone downstream will see.
"""

from __future__ import annotations

import hashlib
import json
import pathlib

import pytest

import tools_label_audit as audit
from validator.devset import CLEAN, load

ROOT = pathlib.Path(__file__).resolve().parent.parent
CORPUS = ROOT / "corpus" / "validator_dev"


def corpus_fingerprint() -> dict:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(CORPUS.glob("*.jsonl"))}


class TestItChangesNothing:

    def test_building_the_audit_leaves_every_corpus_file_byte_identical(self):
        before = corpus_fingerprint()
        audit.build(str(CORPUS))
        assert corpus_fingerprint() == before, (
            "the audit rewrote a corpus file. It exists to hand items to a reviewer, "
            "and a tool that edits the ground truth it is auditing is the thing this "
            "whole exercise is guarding against.")

    def test_rendering_and_writing_the_worksheet_leaves_the_corpus_alone(self, tmp_path):
        before = corpus_fingerprint()
        report = audit.build(str(CORPUS))
        (tmp_path / "a.md").write_text(audit.render(report))
        assert corpus_fingerprint() == before

    def test_the_cli_does_not_touch_the_corpus(self, tmp_path):
        before = corpus_fingerprint()
        assert audit.main(["--corpus", str(CORPUS), "--out", str(tmp_path / "a.md"),
                           "--json", str(tmp_path / "a.json")]) == 0
        assert corpus_fingerprint() == before

    def test_vd_clean_016_is_still_labelled_clean(self):
        """
        Named explicitly. It is the item a reader believes is wrong, which makes
        it the one most likely to be quietly corrected.
        """
        audit.build(str(CORPUS))
        case = next(c for c in load(str(CORPUS)).cases if c.id == "vd-clean-016")
        assert case.label == CLEAN
        assert case.label_status == "unreviewed"
        assert case.reviewers == []

    def test_no_row_carries_a_ruling(self):
        report = audit.build(str(CORPUS))
        for row in report["items"]:
            assert row["ruling"] == "", f"{row['id']} arrived with a ruling already in it"
            assert row["ruled_by"] == ""


class TestItFindsTheKnownCase:

    def test_vd_clean_016_is_flagged(self):
        report = audit.build(str(CORPUS))
        assert "vd-clean-016" in {r["id"] for r in report["items"]}

    def test_the_decisive_word_is_surfaced(self):
        report = audit.build(str(CORPUS))
        row = next(r for r in report["items"] if r["id"] == "vd-clean-016")
        words = {w for c in row["claims"] for w in (c.get("words_not_in_passage") or [])}
        assert "dose" in words, (
            "`dose` is the word that makes this item wrong -- the passage says amount "
            "of drug in the body. If the heuristic stops surfacing it, it has stopped "
            "detecting the one case anyone has confirmed.")

    def test_the_worksheet_reports_where_the_known_case_actually_ranks(self):
        """
        The reading order is a weak proxy and the document must say so with a
        number rather than a promise. Deliberately NOT tuned to rank the known
        answer first: fitting one confirmed case teaches nothing about the rest.
        """
        report = audit.build(str(CORPUS))
        assert report["worked_example"] == "vd-clean-016"
        assert report["worked_rank"] >= 1
        text = audit.render(report)
        assert f"position {report['worked_rank']} of {report['items_flagged']}" in text
        assert "must not be read as severity" in text


class TestWhatItDisplays:

    def test_words_are_shown_as_written_not_as_stems(self):
        """
        Matching is stemmed; a clinician reads the output. A column of `defin`,
        `inflat` and `stat` looks broken and gets ignored, which costs the audit
        its whole value.
        """
        report = audit.build(str(CORPUS))
        shown = {w for r in report["items"] for c in r["claims"]
                 for w in (c.get("words_not_in_passage") or [])}
        for stem_artefact in ("defin", "inflat", "stat", "tissu"):
            assert stem_artefact not in shown

    def test_every_flagged_claim_says_why(self):
        report = audit.build(str(CORPUS))
        for row in report["items"]:
            for claim in row["claims"]:
                assert claim["why"].strip(), f"{row['id']} has a flag with no reason"
                assert claim["kind"] in (audit.ATTRIBUTION_CLAIM, audit.ARITHMETIC_CLAIM,
                                         audit.OPTION_CLAIM)

    def test_the_worksheet_says_it_is_not_an_adjudication(self):
        text = audit.render(audit.build(str(CORPUS)))
        assert "not an adjudication" in text.lower()
        assert "validator/review.py" in text

    def test_it_records_that_the_labels_are_unreviewed(self):
        report = audit.build(str(CORPUS))
        assert report["labels_are_unreviewed"] is True
        for row in report["items"]:
            assert row["label_status"] == "unreviewed"
            assert row["reviewers"] == []

    def test_only_clean_items_are_audited(self):
        """Defective items are supposed to contain defects; flagging them is noise."""
        report = audit.build(str(CORPUS))
        assert all(row["label"] == CLEAN for row in report["items"])


class TestTheHeuristic:

    def test_an_explanation_that_stays_inside_its_passage_is_not_flagged(self):
        class Item:
            source_passage = "Sodium is reabsorbed in the proximal tubule."
            explanation = "Sodium is reabsorbed in the proximal tubule."
            subject = "Physiology"

        class Case:
            id, label, label_status, reviewers, item = "x", CLEAN, "unreviewed", [], Item()

        assert audit.audit_item(Case()) == []

    def test_an_item_with_no_explanation_is_skipped(self):
        class Item:
            source_passage = "Something."
            explanation = ""
            subject = ""

        class Case:
            id, label, label_status, reviewers, item = "x", CLEAN, "unreviewed", [], Item()

        assert audit.audit_item(Case()) == []

    def test_morphological_variants_are_not_reported_as_unbacked(self):
        class Item:
            source_passage = "A drug taken up extensively into tissues leaves the plasma."
            explanation = "The passage states that extensive tissue uptake occurs."
            subject = ""

        class Case:
            id, label, label_status, reviewers, item = "x", CLEAN, "unreviewed", [], Item()

        [claim] = audit.audit_item(Case())
        for word in ("extensive", "tissue"):
            assert word not in claim["words_not_in_passage"], (
                f"{word!r} is in the passage as a variant; reporting it trains the "
                "reviewer to ignore the column")
