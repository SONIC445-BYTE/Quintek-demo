"""
Quintek's question validator, and the evidence about whether it works.

    metrics       the two-armed pass gate, and what an arm of a given size can prove
    devset        the labelled corpora, the three labels, and the contamination guard
    mutate        controlled defect injection: one failure at a time, by construction
    structural    Layer A -- deterministic, free, no false-flag rate
    grounding     Layer B -- is the key supported by the passage supplied with the item
    judge         Layer C -- an independent answer from a model that did not write it
    conformance   Layer D -- is this the question that was asked for
    pipeline      the four layers composed, with every flag attributable to a layer
    analysis      what it got wrong, grouped by the check that got it wrong
    review        the two-reviewer protocol, kappa, and adjudication
    holdout       the one path by which the holdout may be scored, and its ledger
    scripted      providers that are not models: replay for tests, and a ceiling oracle

WHERE THIS SITS
---------------
The measured single-model validator caught 11 of 20 planted defects and
false-flagged 9 of 10 clean items. Nothing in this package claims to have fixed
that. What it provides is the apparatus that would show whether anything had:
a hundred labelled development cases, a sealed holdout with a use ledger, a
gate that judges the lower bound of both arms, and error analysis that names
the layer responsible for each mistake.

No validator has been scored against the holdout. The ledger says so.
"""
