"""
Phase 3: the concept graph.

The tests that matter most here are the ones about NOT merging. A false merge
is close to unrecoverable -- every question, gap and priority signal attached
to either concept is now attached to a concept that does not exist -- while a
false split is visible and fixable with an alias.
"""

from __future__ import annotations

import pytest

from student.concepts import ConceptStore, normalize
from student.db import Database, now_iso


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "q.db")


@pytest.fixture
def store(db):
    return ConceptStore(db)


@pytest.fixture
def user_nb(db):
    uid = db.create_user("l@example.com", "correct-horse")
    db.execute("INSERT INTO notebooks (id,owner_id,title,subject,created_at)"
               " VALUES ('nb-med',?,'Medicine','Medicine',?)", (uid, now_iso()))
    db.execute("INSERT INTO notebooks (id,owner_id,title,subject,created_at)"
               " VALUES ('nb-bio',?,'Biochemistry','Biochemistry',?)", (uid, now_iso()))
    return uid


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def test_case_accents_and_punctuation_are_noise():
    assert normalize("Ferritin") == normalize("ferritin.") == normalize("  FERRITIN  ")
    assert normalize("Ménière") == normalize("meniere")


def test_normalisation_does_not_stem_or_singularise():
    """A stemmer would collapse basis/bases and cell/cells. In medicine that is
    a false merge, which is the thing this module exists to prevent."""
    assert normalize("basis") != normalize("bases")
    assert normalize("cell") != normalize("cells")


# ---------------------------------------------------------------------------
# Resolution: global identity, and the refusal to merge
# ---------------------------------------------------------------------------

def test_the_same_concept_resolves_to_one_global_node(store):
    a = store.resolve_or_create("Ferritin", subject="Biochemistry")
    b = store.resolve_or_create("ferritin")
    c = store.resolve_or_create("  Ferritin. ")
    assert a == b == c


def test_similar_but_distinct_concepts_are_never_auto_merged(store):
    """
    REGRESSION GUARD. `transferrin` and `transferrin saturation` are a
    distinguishable pair -- collapsing them destroys the evidence attached to
    both. Nothing in this module may merge them without an explicit alias.
    """
    a = store.resolve_or_create("transferrin")
    b = store.resolve_or_create("transferrin saturation")
    assert a != b

    pairs = [(x["a_name"], x["b_name"]) for x in store.merge_candidates(threshold=0.4)]
    assert pairs, "these should at least be SUGGESTED for review"
    # ...but suggestion is all it is.
    assert store.find("transferrin") == a
    assert store.find("transferrin saturation") == b


def test_an_alias_is_the_only_way_two_names_become_one_node(store):
    b12 = store.resolve_or_create("Vitamin B12")
    assert store.find("cobalamin") is None
    store.add_alias(b12, "cobalamin")
    assert store.find("cobalamin") == b12
    # And resolving through the alias must not create a second node.
    assert store.resolve_or_create("Cobalamin") == b12


def test_aliasing_a_name_that_belongs_to_another_concept_is_refused(store):
    a = store.resolve_or_create("Ferritin")
    b = store.resolve_or_create("Transferrin")
    with pytest.raises(ValueError, match="already resolves"):
        store.add_alias(a, "Transferrin")
    # Both survive, unmerged.
    assert store.find("Ferritin") == a and store.find("Transferrin") == b


def test_a_later_mention_fills_gaps_but_never_overwrites(store):
    cid = store.resolve_or_create("Ferritin", subject="Biochemistry", description="Iron store")
    store.resolve_or_create("ferritin", subject="Pathology", description="Something else")
    row = store.get(cid)
    assert row["subject"] == "Biochemistry"
    assert row["description"] == "Iron store"


def test_a_concept_needs_a_name(store):
    with pytest.raises(ValueError):
        store.resolve_or_create("   ")


# ---------------------------------------------------------------------------
# Relationships
# ---------------------------------------------------------------------------

def test_relationships_use_a_known_vocabulary(store):
    a = store.resolve_or_create("Hepcidin")
    b = store.resolve_or_create("Iron absorption")
    store.relate(a, b, "mechanism_of", confidence=0.8)
    with pytest.raises(ValueError, match="unknown relation type"):
        store.relate(a, b, "vaguely_about")


def test_a_self_relationship_is_dropped(store):
    a = store.resolve_or_create("Ferritin")
    store.relate(a, a, "related_to")
    assert store.neighbours(a) == []


def test_relationships_are_not_duplicated(store):
    a = store.resolve_or_create("A")
    b = store.resolve_or_create("B")
    store.relate(a, b, "causes")
    store.relate(a, b, "causes")
    assert len(store.neighbours(a)) == 1


def test_neighbours_include_both_directions(store):
    a = store.resolve_or_create("Anaemia")
    b = store.resolve_or_create("Fatigue")
    store.relate(a, b, "manifestation_of")
    assert {n["dir"] for n in store.neighbours(a)} == {"out"}
    assert {n["dir"] for n in store.neighbours(b)} == {"in"}


# ---------------------------------------------------------------------------
# Cross-notebook behaviour -- the reason concepts are global
# ---------------------------------------------------------------------------

def test_one_concept_can_belong_to_two_notebooks(store, user_nb):
    """Hb in Biochemistry is the same node a Medicine question references."""
    hb = store.resolve_or_create("Haemoglobin", subject="Biochemistry")
    store.link_to_notebook("nb-bio", hb, "primary")
    store.link_to_notebook("nb-med", hb, "supporting")
    titles = {n["title"] for n in store.notebooks_for(hb, user_nb)}
    assert titles == {"Medicine", "Biochemistry"}


def test_graph_flags_cross_subject_edges(store, user_nb):
    hb = store.resolve_or_create("Haemoglobin", subject="Biochemistry")
    jaundice = store.resolve_or_create("Jaundice", subject="Medicine")
    store.link_to_notebook("nb-bio", hb)
    store.link_to_notebook("nb-med", jaundice)
    store.relate(hb, jaundice, "associated_with")

    graph = store.graph_for_user(user_nb)
    assert len(graph["nodes"]) == 2
    assert len(graph["edges"]) == 1
    assert graph["edges"][0]["cross_subject"] is True


def test_graph_is_scoped_to_the_owner(store, db):
    mine = db.create_user("a@example.com", "correct-horse")
    theirs = db.create_user("b@example.com", "correct-horse")
    db.execute("INSERT INTO notebooks (id,owner_id,title,created_at) VALUES ('n1',?,'Mine',?)",
               (mine, now_iso()))
    db.execute("INSERT INTO notebooks (id,owner_id,title,created_at) VALUES ('n2',?,'Theirs',?)",
               (theirs, now_iso()))
    c = store.resolve_or_create("Shared concept")
    store.link_to_notebook("n2", c)
    assert store.graph_for_user(mine)["nodes"] == []
    assert len(store.graph_for_user(theirs)["nodes"]) == 1
