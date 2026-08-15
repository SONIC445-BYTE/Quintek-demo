# Cross-Subject Concept Graph Evaluation

Every item can have:
- primary concept
- supporting concepts
- cross-subject relationships

Example:

Primary:
`medicine.anemia.iron_deficiency`

Supporting:
`biochemistry.iron.metabolism`
`physiology.oxygen_transport`
`pathology.erythropoiesis`

## Required metrics

1. Primary concept precision/recall
2. Supporting concept recall
3. False cross-subject link rate
4. Edge F1
5. Wrong-subject attribution rate

A question mentioning Hb does not automatically make Hb a Medicine concept. The graph should represent the concept itself and its source/notebook references separately.

This preserves the intended architecture:
- notebooks organize sources
- concepts are shared
- questions can traverse concepts across notebooks.
