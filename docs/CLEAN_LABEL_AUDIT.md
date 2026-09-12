# Clean-label audit — corpus/validator_dev

**This is not an adjudication.** Nothing here rules on any item and no label has
been changed. `28` of `40` clean items carry a claim worth a
clinician's eye; the rest were not flagged by any of the three tests below.

Every label in this corpus is `label_status: unreviewed`, `gold_standard: false`,
`provenance: model_authored`, with zero reviewers. That is the reason this
worksheet exists — not a suspicion about any particular item.

To record a ruling, use `validator/review.py` (two named reviewers, blind, kappa
reported). Writing in the **Ruling** boxes below is a note, not a review.

## The worked example

`vd-clean-016` was flagged by the validator as a false positive under
`grounding/explanation_contradicts_passage`. Reading it, the check was right:

> **Passage:** The apparent volume of distribution is the amount of drug **in the
> body** divided by its plasma concentration.
>
> **Explanation:** The passage defines the apparent volume of distribution as the
> ratio of **dose** to plasma concentration…

Amount-in-body over Cp and dose over Cp are different quantities; they coincide
only at t=0 for an IV bolus with complete bioavailability. The explanation also
attributes the wrong definition to a passage in the same item, which is a claim
about a text, not a matter of emphasis.

**That is one reader's view, offered for calibration, and it is not a ruling.**
It is included so a reviewer can see what the tests are aiming at and judge
whether they are aimed correctly.

### How well the reading order works — measured, not claimed

`vd-clean-016` is the only item here anyone has confirmed is wrong, and it sits
at **position 9 of 28** in the order below.

So the order is a weak proxy and must not be read as severity. It ranks by how
many words and figures an explanation asserts that its passage does not carry,
and the thing that made `vd-clean-016` wrong was a **single** word — *dose*
where the passage says *amount of drug in the body* — in a sentence that was
otherwise well grounded. Counting cannot see that, and the order was
deliberately not tuned to put the known answer first: fitting the one case we
know would tell us nothing about the twenty-seven we do not.

Read it as a queue for an hour you have, not a ranking. A clean sweep of all
28 is the only thing that settles the arm.

## What was tested

| Test | Meaning |
|---|---|
| `attribution` | the explanation says "the passage defines/states/gives X" — a claim about a text in the item, so it is either accurate or not |
| `arithmetic` | the explanation asserts a figure the passage does not contain, usually by applying a rule it states. Often correct; flagged because the grounding check has no category for "entailed but not present" |
| `options` | the explanation makes claims about numbered options the passage never mentions by number |

Counts: {"arithmetic": 3, "attribution": 26, "options": 10}

---

## 1. vd-clean-025  ·  Surgery

`label: CLEAN` · `label_status: unreviewed` · `reviewers: 0`

**Passage.** Early appendiceal inflammation distends the organ and produces visceral pain, which is referred to the midgut dermatome around the umbilicus and is poorly localised and colicky. As inflammation extends to the parietal peritoneum overlying the appendix, somatic afferents are stimulated and the pain shifts to the right iliac fossa, where it becomes sharp and well localised. Right hypochondrial pain radiating to the right scapular tip suggests biliary disease, and epigastric pain boring through to the back suggests pancreatitis.

**Explanation.** The passage explains the migration by the change from visceral midgut referral to somatic parietal irritation, giving peri-umbilical pain first and right iliac fossa pain later. Options 3 and 4 describe biliary and pancreatic patterns named separately in the passage.

**Claim 1 — `attribution`**

> The passage explains the migration by the change from visceral midgut referral to somatic parietal irritation, giving peri-umbilical pain first and right iliac fossa pain later.

Nearest passage sentence:

> As inflammation extends to the parietal peritoneum overlying the appendix, somatic afferents are stimulated and the pain shifts to the right iliac fossa, where it becomes sharp and well localised.

Words asserted that the passage nowhere uses: `change, first, giving, irritation, later, migration, peri, referral, umbilical`

*Why flagged:* the explanation attributes a definition or statement to the passage; compare the two sentences and decide whether the attribution is accurate

**Claim 2 — `options`**

> Options 3 and 4 describe biliary and pancreatic patterns named separately in the passage.

*Why flagged:* the explanation makes a claim about numbered options; the passage never refers to options by number, so a checker comparing the two has nothing to match against

**Ruling** (clean / defective / edge): ______  **By:** ______  **Note:** ______


---

## 2. vd-clean-007  ·  Biochemistry

`label: CLEAN` · `label_status: unreviewed` · `reviewers: 0`

**Passage.** Hepatic glucose-6-phosphatase catalyses the final step common to glycogenolysis and gluconeogenesis, removing the phosphate so that free glucose can leave the hepatocyte. When the enzyme is absent, glucose-6-phosphate accumulates and cannot be exported, so fasting blood glucose falls despite abundant stored glycogen. The accumulated substrate is diverted to lactate, and glycogen distends the liver.

**Explanation.** The passage states that glucose-6-phosphatase performs the final, shared step of both glycogenolysis and gluconeogenesis, so its absence traps glucose inside the hepatocyte as the phosphate ester. Options 2 to 4 name enzymes and pathways that are not the deficient step.

**Claim 1 — `attribution`**

> The passage states that glucose-6-phosphatase performs the final, shared step of both glycogenolysis and gluconeogenesis, so its absence traps glucose inside the hepatocyte as the phosphate ester.

Nearest passage sentence:

> Hepatic glucose-6-phosphatase catalyses the final step common to glycogenolysis and gluconeogenesis, removing the phosphate so that free glucose can leave the hepatocyte.

Words asserted that the passage nowhere uses: `absence, both, ester, inside, performs, shared, traps`

*Why flagged:* the explanation attributes a definition or statement to the passage; compare the two sentences and decide whether the attribution is accurate

**Claim 2 — `options`**

> Options 2 to 4 name enzymes and pathways that are not the deficient step.

*Why flagged:* the explanation makes a claim about numbered options; the passage never refers to options by number, so a checker comparing the two has nothing to match against

**Ruling** (clean / defective / edge): ______  **By:** ______  **Note:** ______


---

## 3. vd-clean-019  ·  Microbiology

`label: CLEAN` · `label_status: unreviewed` · `reviewers: 0`

**Passage.** In enteric fever the organism is present in the bloodstream from the end of the incubation period, and bacteraemia is maximal during the first week of illness, when blood culture is positive in the large majority of untreated cases. Stool and urine cultures become positive later, typically from the second and third weeks. The Widal test measures antibody, which takes time to appear, so a single early titre is of little diagnostic value.

**Explanation.** The passage states that bacteraemia is at its peak in the first week and that blood culture yield is highest then, while stool and urine yields rise later and the Widal test needs a rise in titre over time.

**Claim 1 — `attribution`**

> The passage states that bacteraemia is at its peak in the first week and that blood culture yield is highest then, while stool and urine yields rise later and the Widal test needs a rise in titre over time.

Nearest passage sentence:

> Stool and urine cultures become positive later, typically from the second and third weeks.

Words asserted that the passage nowhere uses: `highest, needs, over, peak, rise, while, yield`

*Why flagged:* the explanation attributes a definition or statement to the passage; compare the two sentences and decide whether the attribution is accurate

**Ruling** (clean / defective / edge): ______  **By:** ______  **Note:** ______


---

## 4. vd-clean-027  ·  Surgery

`label: CLEAN` · `label_status: unreviewed` · `reviewers: 0`

**Passage.** The Parkland formula estimates the crystalloid requirement in the first 24 hours after a major burn as 4 millilitres of Ringer lactate per kilogram of body weight per per cent of total body surface area burned. Half of the calculated volume is given in the first 8 hours measured from the time of injury, not from the time of admission, and the remaining half over the following 16 hours. The formula is a starting estimate and is titrated against urine output.

**Explanation.** The passage gives 4 millilitres per kilogram per per cent burn: 4 times 70 times 30 equals 8400 millilitres, with half in the first 8 hours from the time of injury.

**Claim 1 — `arithmetic`**

> The passage gives 4 millilitres per kilogram per per cent burn: 4 times 70 times 30 equals 8400 millilitres, with half in the first 8 hours from the time of injury.

Nearest passage sentence:

> The Parkland formula estimates the crystalloid requirement in the first 24 hours after a major burn as 4 millilitres of Ringer lactate per kilogram of body weight per per cent of total body surface area burned.

Figures not in the passage: `30, 70, 8400`

*Why flagged:* the explanation asserts a figure the passage does not contain, usually by applying a rule the passage states. Often correct and good practice: flagged because the grounding check has no category for a claim the passage ENTAILS but does not carry, so it may be reported as a contradiction

**Claim 2 — `attribution`**

> The passage gives 4 millilitres per kilogram per per cent burn: 4 times 70 times 30 equals 8400 millilitres, with half in the first 8 hours from the time of injury.

Nearest passage sentence:

> The Parkland formula estimates the crystalloid requirement in the first 24 hours after a major burn as 4 millilitres of Ringer lactate per kilogram of body weight per per cent of total body surface area burned.

Words asserted that the passage nowhere uses: `equals`

*Why flagged:* the explanation attributes a definition or statement to the passage; compare the two sentences and decide whether the attribution is accurate

**Ruling** (clean / defective / edge): ______  **By:** ______  **Note:** ______


---

## 5. vd-clean-032  ·  Obstetrics and Gynaecology

`label: CLEAN` · `label_status: unreviewed` · `reviewers: 0`

**Passage.** Naegele's rule estimates the expected date of delivery from the first day of the last menstrual period by adding seven days and then subtracting three months, adding a year where the subtraction crosses into the previous year. The rule assumes a regular 28-day cycle with ovulation on day 14, and gives a gestation of 280 days from the first day of the last period.

**Explanation.** The passage gives the rule as add seven days and subtract three months. 10 March plus seven days is 17 March; subtracting three months gives 17 December of the same year.

**Claim 1 — `arithmetic`**

> 10 March plus seven days is 17 March;

Nearest passage sentence:

> Naegele's rule estimates the expected date of delivery from the first day of the last menstrual period by adding seven days and then subtracting three months, adding a year where the subtraction crosses into the previous year.

Figures not in the passage: `10, 17`

*Why flagged:* the explanation asserts a figure the passage does not contain, usually by applying a rule the passage states. Often correct and good practice: flagged because the grounding check has no category for a claim the passage ENTAILS but does not carry, so it may be reported as a contradiction

**Claim 2 — `arithmetic`**

> subtracting three months gives 17 December of the same year.

Nearest passage sentence:

> Naegele's rule estimates the expected date of delivery from the first day of the last menstrual period by adding seven days and then subtracting three months, adding a year where the subtraction crosses into the previous year.

Figures not in the passage: `17`

*Why flagged:* the explanation asserts a figure the passage does not contain, usually by applying a rule the passage states. Often correct and good practice: flagged because the grounding check has no category for a claim the passage ENTAILS but does not carry, so it may be reported as a contradiction

**Claim 3 — `attribution`**

> The passage gives the rule as add seven days and subtract three months.

Nearest passage sentence:

> Naegele's rule estimates the expected date of delivery from the first day of the last menstrual period by adding seven days and then subtracting three months, adding a year where the subtraction crosses into the previous year.

Words asserted that the passage nowhere uses: `add`

*Why flagged:* the explanation attributes a definition or statement to the passage; compare the two sentences and decide whether the attribution is accurate

**Ruling** (clean / defective / edge): ______  **By:** ______  **Note:** ______


---

## 6. vd-clean-035  ·  Paediatrics

`label: CLEAN` · `label_status: unreviewed` · `reviewers: 0`

**Passage.** Kawasaki disease is an acute vasculitis of unknown cause, most often affecting children under five, characterised by fever lasting at least five days with conjunctival injection, mucosal changes, rash, extremity changes and cervical lymphadenopathy. Its serious consequence is coronary arteritis with aneurysm formation, which occurs in around a quarter of untreated children. Intravenous immunoglobulin given within the first ten days of illness reduces that risk substantially. It is unrelated to rheumatic fever, which follows group A streptococcal pharyngitis.

**Explanation.** The passage gives aneurysm prevention as the purpose of treatment and states that the aetiology is unknown, which excludes eradication of an organism. Rheumatic carditis is a separate disease named in the passage for contrast.

**Claim 1 — `attribution`**

> The passage gives aneurysm prevention as the purpose of treatment and states that the aetiology is unknown, which excludes eradication of an organism.

Nearest passage sentence:

> Its serious consequence is coronary arteritis with aneurysm formation, which occurs in around a quarter of untreated children.

Words asserted that the passage nowhere uses: `aetiology, eradication, excludes, organism, prevention, purpose, treatment`

*Why flagged:* the explanation attributes a definition or statement to the passage; compare the two sentences and decide whether the attribution is accurate

**Ruling** (clean / defective / edge): ______  **By:** ______  **Note:** ______


---

## 7. vd-clean-015  ·  Pharmacology

`label: CLEAN` · `label_status: unreviewed` · `reviewers: 0`

**Passage.** Warfarin inhibits vitamin K epoxide reductase, preventing regeneration of reduced vitamin K and so preventing gamma-carboxylation of the glutamate residues of factors II, VII, IX and X. Administering vitamin K restores the supply of the reduced cofactor and allows carboxylation to resume, but new functional factors must still be synthesised, so correction takes hours. Immediate replacement of preformed factors requires prothrombin complex concentrate or fresh frozen plasma.

**Explanation.** The passage states that warfarin acts by blocking the vitamin K cycle and that supplying vitamin K restores carboxylation, and separately notes that only prothrombin complex concentrate or plasma supplies preformed factors.

**Claim 1 — `attribution`**

> The passage states that warfarin acts by blocking the vitamin K cycle and that supplying vitamin K restores carboxylation, and separately notes that only prothrombin complex concentrate or plasma supplies preformed factors.

Nearest passage sentence:

> Immediate replacement of preformed factors requires prothrombin complex concentrate or fresh frozen plasma.

Words asserted that the passage nowhere uses: `acts, blocking, cycle, only, separately, supplying`

*Why flagged:* the explanation attributes a definition or statement to the passage; compare the two sentences and decide whether the attribution is accurate

**Ruling** (clean / defective / edge): ______  **By:** ______  **Note:** ______


---

## 8. vd-clean-040  ·  PSM

`label: CLEAN` · `label_status: unreviewed` · `reviewers: 0`

**Passage.** Randomisation allocates participants to trial arms by chance, so that on average both measured and unmeasured prognostic factors are distributed comparably between the groups. This is its distinctive contribution, because no analytical adjustment can control for a confounder that was never measured. Concealment of the allocation from the investigator is achieved by blinding, equality of group sizes by block randomisation, and representativeness of the study population by the eligibility criteria, none of which is the purpose of randomisation itself.

**Explanation.** The passage states that randomisation addresses confounding, including confounders that have not been measured, and separately identifies blinding, block sizes and eligibility criteria as the devices responsible for options 2, 3 and 4.

**Claim 1 — `attribution`**

> The passage states that randomisation addresses confounding, including confounders that have not been measured, and separately identifies blinding, block sizes and eligibility criteria as the devices responsible for options 2, 3 and 4.

Nearest passage sentence:

> Concealment of the allocation from the investigator is achieved by blinding, equality of group sizes by block randomisation, and representativeness of the study population by the eligibility criteria, none of which is the purpose of randomisation itself.

Words asserted that the passage nowhere uses: `addresses, confounding, devices, including, responsible, separately`

*Why flagged:* the explanation attributes a definition or statement to the passage; compare the two sentences and decide whether the attribution is accurate

**Claim 2 — `options`**

> The passage states that randomisation addresses confounding, including confounders that have not been measured, and separately identifies blinding, block sizes and eligibility criteria as the devices responsible for options 2, 3 and 4.

*Why flagged:* the explanation makes a claim about numbered options; the passage never refers to options by number, so a checker comparing the two has nothing to match against

**Ruling** (clean / defective / edge): ______  **By:** ______  **Note:** ______


---

## 9. vd-clean-016  ·  Pharmacology

`label: CLEAN` · `label_status: unreviewed` · `reviewers: 0`

**Passage.** The apparent volume of distribution is the amount of drug in the body divided by its plasma concentration. It is not an anatomical volume. A drug taken up extensively into tissues leaves little in the plasma, so the calculated volume can exceed total body water by many times. A drug that remains in plasma, for example because it is highly bound to albumin, has a small apparent volume of distribution.

**Explanation.** The passage defines the apparent volume of distribution as the ratio of dose to plasma concentration and states that extensive tissue uptake lowers the plasma concentration and so inflates the calculated volume. High plasma protein binding keeps drug in plasma and does the opposite.

**Claim 1 — `attribution`**

> The passage defines the apparent volume of distribution as the ratio of dose to plasma concentration and states that extensive tissue uptake lowers the plasma concentration and so inflates the calculated volume.

Nearest passage sentence:

> The apparent volume of distribution is the amount of drug in the body divided by its plasma concentration.

Words asserted that the passage nowhere uses: `dose, inflates, lowers, ratio, uptake`

*Why flagged:* the explanation attributes a definition or statement to the passage; compare the two sentences and decide whether the attribution is accurate

**Ruling** (clean / defective / edge): ______  **By:** ______  **Note:** ______


---

## 10. vd-clean-033  ·  Paediatrics

`label: CLEAN` · `label_status: unreviewed` · `reviewers: 0`

**Passage.** Physiological jaundice of the newborn reflects the combination of a high red cell mass with a short red cell lifespan and immature hepatic conjugation. In a term infant it appears after the first 24 hours, usually on the second or third day, peaks around the third to fifth day, is predominantly unconjugated, and resolves by the end of the first week. Jaundice appearing within the first 24 hours of life is never physiological and requires investigation for haemolysis or sepsis.

**Explanation.** The passage states that physiological jaundice never appears in the first 24 hours and that jaundice at that time is pathological until proved otherwise. Options 2 to 4 are given in the passage as features of the physiological pattern.

**Claim 1 — `attribution`**

> The passage states that physiological jaundice never appears in the first 24 hours and that jaundice at that time is pathological until proved otherwise.

Nearest passage sentence:

> Jaundice appearing within the first 24 hours of life is never physiological and requires investigation for haemolysis or sepsis.

Words asserted that the passage nowhere uses: `otherwise, pathological, proved, time, until`

*Why flagged:* the explanation attributes a definition or statement to the passage; compare the two sentences and decide whether the attribution is accurate

**Claim 2 — `options`**

> Options 2 to 4 are given in the passage as features of the physiological pattern.

*Why flagged:* the explanation makes a claim about numbered options; the passage never refers to options by number, so a checker comparing the two has nothing to match against

**Ruling** (clean / defective / edge): ______  **By:** ______  **Note:** ______


---

## 11. vd-clean-034  ·  Paediatrics

`label: CLEAN` · `label_status: unreviewed` · `reviewers: 0`

**Passage.** In secretory diarrhoea such as cholera, enterotoxin drives chloride and water secretion from the crypt cells, but the sodium-glucose cotransporter of the villous enterocyte remains functional. When sodium and glucose are presented together in the lumen in roughly equimolar amounts they are absorbed together, and water follows osmotically, so net absorption can exceed ongoing secretion. The currently recommended solution is hypo-osmolar relative to plasma, which reduces stool output compared with the older isotonic formulation.

**Explanation.** The passage identifies the sodium-glucose cotransporter as intact even when the secretory pathway is activated, so co-administered sodium and glucose are absorbed together and water follows. The passage also states that modern ORS is hypo-osmolar, contradicting option 3.

**Claim 1 — `attribution`**

> The passage identifies the sodium-glucose cotransporter as intact even when the secretory pathway is activated, so co-administered sodium and glucose are absorbed together and water follows.

Nearest passage sentence:

> When sodium and glucose are presented together in the lumen in roughly equimolar amounts they are absorbed together, and water follows osmotically, so net absorption can exceed ongoing secretion.

Words asserted that the passage nowhere uses: `activated, administered, even, intact, pathway`

*Why flagged:* the explanation attributes a definition or statement to the passage; compare the two sentences and decide whether the attribution is accurate

**Claim 2 — `options`**

> The passage also states that modern ORS is hypo-osmolar, contradicting option 3.

*Why flagged:* the explanation makes a claim about numbered options; the passage never refers to options by number, so a checker comparing the two has nothing to match against

**Ruling** (clean / defective / edge): ______  **By:** ______  **Note:** ______


---

## 12. vd-clean-038  ·  PSM

`label: CLEAN` · `label_status: unreviewed` · `reviewers: 0`

**Passage.** Sensitivity and specificity describe how the test performs among those with and without the disease respectively, and are properties of the test that do not change with the prevalence of disease in the population tested. Predictive values do change. As prevalence falls, the number of true positives falls while the number of false positives generated from the larger disease-free group does not, so a smaller proportion of all positive results are true and the positive predictive value declines.

**Explanation.** The passage states that sensitivity and specificity are properties of the test itself and do not vary with prevalence, whereas predictive values do, and that a lower prevalence means a larger proportion of positives are false.

**Claim 1 — `attribution`**

> The passage states that sensitivity and specificity are properties of the test itself and do not vary with prevalence, whereas predictive values do, and that a lower prevalence means a larger proportion of positives are false.

Nearest passage sentence:

> As prevalence falls, the number of true positives falls while the number of false positives generated from the larger disease-free group does not, so a smaller proportion of all positive results are true and the positive predictive value declines.

Words asserted that the passage nowhere uses: `itself, lower, means, vary, whereas`

*Why flagged:* the explanation attributes a definition or statement to the passage; compare the two sentences and decide whether the attribution is accurate

**Ruling** (clean / defective / edge): ______  **By:** ______  **Note:** ______


---

## 13. vd-clean-008  ·  Biochemistry

`label: CLEAN` · `label_status: unreviewed` · `reviewers: 0`

**Passage.** A competitive inhibitor binds the active site and competes with substrate for it. Because sufficiently high substrate concentration displaces the inhibitor, the maximal velocity of the reaction is still attainable and Vmax is unchanged. More substrate is, however, needed to reach half of that maximum, so the apparent Km rises.

**Explanation.** The passage states that a competitive inhibitor can be displaced by excess substrate, so maximal velocity is still reachable, while more substrate is needed to reach half-maximal velocity. That is an unchanged Vmax with a raised apparent Km.

**Claim 1 — `attribution`**

> The passage states that a competitive inhibitor can be displaced by excess substrate, so maximal velocity is still reachable, while more substrate is needed to reach half-maximal velocity.

Nearest passage sentence:

> Because sufficiently high substrate concentration displaces the inhibitor, the maximal velocity of the reaction is still attainable and Vmax is unchanged.

Words asserted that the passage nowhere uses: `can, excess, reachable, while`

*Why flagged:* the explanation attributes a definition or statement to the passage; compare the two sentences and decide whether the attribution is accurate

**Ruling** (clean / defective / edge): ______  **By:** ______  **Note:** ______


---

## 14. vd-clean-018  ·  Microbiology

`label: CLEAN` · `label_status: unreviewed` · `reviewers: 0`

**Passage.** Chlamydia trachomatis is a bacterium with a cell wall and both DNA and RNA, but it is an obligate intracellular organism: its developmental cycle, from infectious elementary body to replicating reticulate body, takes place only within a host cell vacuole. It therefore cannot be grown on cell-free bacteriological media and requires cell culture or nucleic acid amplification for laboratory diagnosis.

**Explanation.** The passage states that the organism is a bacterium which completes its developmental cycle only inside a host cell, and therefore requires cell culture rather than cell-free medium.

**Claim 1 — `attribution`**

> The passage states that the organism is a bacterium which completes its developmental cycle only inside a host cell, and therefore requires cell culture rather than cell-free medium.

Nearest passage sentence:

> Chlamydia trachomatis is a bacterium with a cell wall and both DNA and RNA, but it is an obligate intracellular organism: its developmental cycle, from infectious elementary body to replicating reticulate body, takes place only within a host cell vacuole.

Words asserted that the passage nowhere uses: `completes, inside, medium, rather`

*Why flagged:* the explanation attributes a definition or statement to the passage; compare the two sentences and decide whether the attribution is accurate

**Ruling** (clean / defective / edge): ______  **By:** ______  **Note:** ______


---

## 15. vd-clean-021  ·  Medicine

`label: CLEAN` · `label_status: unreviewed` · `reviewers: 0`

**Passage.** Nephrotic syndrome is defined by proteinuria exceeding 3.5 grams per 24 hours in an adult, together with hypoalbuminaemia, oedema and hyperlipidaemia. Renal function may be normal at presentation. An active urinary sediment with red cells and red cell casts points instead to a nephritic process, in which proteinuria is usually milder.

**Explanation.** The passage gives heavy proteinuria above 3.5 grams daily as the defining feature. Red cell casts are named in the passage as a nephritic finding, and neither the creatinine nor the blood pressure is part of the definition.

**Claim 1 — `attribution`**

> The passage gives heavy proteinuria above 3.5 grams daily as the defining feature.

Nearest passage sentence:

> Nephrotic syndrome is defined by proteinuria exceeding 3.5 grams per 24 hours in an adult, together with hypoalbuminaemia, oedema and hyperlipidaemia.

Words asserted that the passage nowhere uses: `above, daily, feature, heavy`

*Why flagged:* the explanation attributes a definition or statement to the passage; compare the two sentences and decide whether the attribution is accurate

**Ruling** (clean / defective / edge): ______  **By:** ______  **Note:** ______


---

## 16. vd-clean-009  ·  Pathology

`label: CLEAN` · `label_status: unreviewed` · `reviewers: 0`

**Passage.** Caseous necrosis describes a friable, cheese-like area in which tissue architecture is completely obliterated, leaving amorphous granular debris enclosed by a rim of epithelioid macrophages and lymphocytes. It is characteristic of the granuloma of tuberculosis. Liquefactive necrosis follows infarction in the brain and suppurative infection, fibrinoid necrosis occurs in vessel walls in immune injury, and fat necrosis follows lipase release in pancreatitis.

**Explanation.** The passage identifies the cheese-like, structureless centre of the tuberculous granuloma as caseous necrosis. The other patterns are named in the passage as occurring in different settings.

**Claim 1 — `attribution`**

> The passage identifies the cheese-like, structureless centre of the tuberculous granuloma as caseous necrosis.

Nearest passage sentence:

> Caseous necrosis describes a friable, cheese-like area in which tissue architecture is completely obliterated, leaving amorphous granular debris enclosed by a rim of epithelioid macrophages and lymphocytes.

Words asserted that the passage nowhere uses: `centre, structureless, tuberculous`

*Why flagged:* the explanation attributes a definition or statement to the passage; compare the two sentences and decide whether the attribution is accurate

**Ruling** (clean / defective / edge): ______  **By:** ______  **Note:** ______


---

## 17. vd-clean-012  ·  Pathology

`label: CLEAN` · `label_status: unreviewed` · `reviewers: 0`

**Passage.** Metaplasia is a reversible change in which one differentiated cell type is replaced by another differentiated cell type better able to withstand a chronic stress. In chronic gastro-oesophageal reflux the stratified squamous epithelium of the lower oesophagus is replaced by intestinal-type columnar epithelium, known as Barrett oesophagus. Metaplasia is not itself malignant, although the altered epithelium carries an increased risk of subsequent dysplasia.

**Explanation.** The passage defines metaplasia as the replacement of one differentiated cell type by another and gives this exact oesophageal example. Dysplasia denotes disordered growth with atypia, which the passage distinguishes from metaplasia.

**Claim 1 — `attribution`**

> The passage defines metaplasia as the replacement of one differentiated cell type by another and gives this exact oesophageal example.

Nearest passage sentence:

> Metaplasia is a reversible change in which one differentiated cell type is replaced by another differentiated cell type better able to withstand a chronic stress.

Words asserted that the passage nowhere uses: `exact, example, replacement`

*Why flagged:* the explanation attributes a definition or statement to the passage; compare the two sentences and decide whether the attribution is accurate

**Ruling** (clean / defective / edge): ______  **By:** ______  **Note:** ______


---

## 18. vd-clean-013  ·  Pharmacology

`label: CLEAN` · `label_status: unreviewed` · `reviewers: 0`

**Passage.** Under first-order elimination the rate of elimination is directly proportional to the plasma concentration, so a constant fraction of the drug present is removed per unit time and the half-life is independent of concentration. Under zero-order elimination the eliminating system is saturated, a constant amount is removed per unit time, and the apparent half-life lengthens as concentration rises.

**Explanation.** The passage defines first-order elimination as rate proportional to concentration, which is the same as a constant fraction per unit time, and contrasts it with the constant amount of zero-order kinetics.

**Claim 1 — `attribution`**

> The passage defines first-order elimination as rate proportional to concentration, which is the same as a constant fraction per unit time, and contrasts it with the constant amount of zero-order kinetics.

Nearest passage sentence:

> Under first-order elimination the rate of elimination is directly proportional to the plasma concentration, so a constant fraction of the drug present is removed per unit time and the half-life is independent of concentration.

Words asserted that the passage nowhere uses: `contrasts, kinetics, same`

*Why flagged:* the explanation attributes a definition or statement to the passage; compare the two sentences and decide whether the attribution is accurate

**Ruling** (clean / defective / edge): ______  **By:** ______  **Note:** ______


---

## 19. vd-clean-026  ·  Surgery

`label: CLEAN` · `label_status: unreviewed` · `reviewers: 0`

**Passage.** When the small bowel lumen is mechanically occluded, swallowed air and the several litres of gastrointestinal secretion produced each day accumulate in the segment proximal to the obstruction, which distends progressively. Bowel distal to the obstruction empties and collapses, which is the basis of the radiological contrast between dilated proximal and collapsed distal loops. Peristalsis is initially increased proximally, producing colic, and fails only later. Perforation is a late complication of an ischaemic, over-distended segment.

**Explanation.** The passage states that bowel proximal to the block distends with swallowed air and unabsorbed secretion while the distal bowel collapses. Perforation is described as a late complication, not the cause of the distension.

**Claim 1 — `attribution`**

> The passage states that bowel proximal to the block distends with swallowed air and unabsorbed secretion while the distal bowel collapses.

Nearest passage sentence:

> When the small bowel lumen is mechanically occluded, swallowed air and the several litres of gastrointestinal secretion produced each day accumulate in the segment proximal to the obstruction, which distends progressively.

Words asserted that the passage nowhere uses: `block, unabsorbed, while`

*Why flagged:* the explanation attributes a definition or statement to the passage; compare the two sentences and decide whether the attribution is accurate

**Ruling** (clean / defective / edge): ______  **By:** ______  **Note:** ______


---

## 20. vd-clean-003  ·  Physiology

`label: CLEAN` · `label_status: unreviewed` · `reviewers: 0`

**Passage.** Carotid sinus baroreceptors discharge at a rate proportional to arterial wall stretch. Their afferent traffic inhibits the sympathetic vasomotor centre and excites vagal outflow. When arterial pressure falls, firing decreases, that inhibition is withdrawn, and sympathetic outflow rises, producing tachycardia and vasoconstriction within a few heartbeats.

**Explanation.** The passage states that baroreceptor firing is proportional to stretch and that reduced firing releases the vasomotor centre from inhibition. Lower pressure therefore means less firing, more sympathetic outflow, tachycardia and vasoconstriction.

**Claim 1 — `attribution`**

> The passage states that baroreceptor firing is proportional to stretch and that reduced firing releases the vasomotor centre from inhibition.

Nearest passage sentence:

> Carotid sinus baroreceptors discharge at a rate proportional to arterial wall stretch.

Words asserted that the passage nowhere uses: `reduced, releases`

*Why flagged:* the explanation attributes a definition or statement to the passage; compare the two sentences and decide whether the attribution is accurate

**Ruling** (clean / defective / edge): ______  **By:** ______  **Note:** ______


---

## 21. vd-clean-005  ·  Biochemistry

`label: CLEAN` · `label_status: unreviewed` · `reviewers: 0`

**Passage.** Glycolysis has three irreversible, regulated steps, catalysed by hexokinase, phosphofructokinase-1 and pyruvate kinase. Of these, phosphofructokinase-1 catalyses the committed step and is the rate-limiting enzyme of the pathway. It is inhibited by ATP and citrate and activated by AMP and fructose-2,6-bisphosphate.

**Explanation.** The passage names phosphofructokinase-1 as the committed, rate-limiting step. Hexokinase and pyruvate kinase are also regulated but the passage identifies PFK-1 as rate-limiting.

**Claim 1 — `attribution`**

> Hexokinase and pyruvate kinase are also regulated but the passage identifies PFK-1 as rate-limiting.

Nearest passage sentence:

> Glycolysis has three irreversible, regulated steps, catalysed by hexokinase, phosphofructokinase-1 and pyruvate kinase.

Words asserted that the passage nowhere uses: `also, pfk`

*Why flagged:* the explanation attributes a definition or statement to the passage; compare the two sentences and decide whether the attribution is accurate

**Ruling** (clean / defective / edge): ______  **By:** ______  **Note:** ______


---

## 22. vd-clean-011  ·  Pathology

`label: CLEAN` · `label_status: unreviewed` · `reviewers: 0`

**Passage.** Light's criteria classify pleural fluid as an exudate if any one of the following is met: fluid-to-serum protein ratio greater than 0.5, fluid-to-serum lactate dehydrogenase ratio greater than 0.6, or fluid LDH greater than two-thirds of the upper limit of the normal serum range. Fluid meeting none of the three is a transudate. Meeting a single criterion is sufficient for the exudate label.

**Explanation.** The passage gives the thresholds as a protein ratio above 0.5 or an LDH ratio above 0.6, and states that meeting any one is sufficient. Both ratios here exceed their thresholds, so the fluid is an exudate.

**Claim 1 — `attribution`**

> The passage gives the thresholds as a protein ratio above 0.5 or an LDH ratio above 0.6, and states that meeting any one is sufficient.

Nearest passage sentence:

> Light's criteria classify pleural fluid as an exudate if any one of the following is met: fluid-to-serum protein ratio greater than 0.5, fluid-to-serum lactate dehydrogenase ratio greater than 0.6, or fluid LDH greater than two-thirds of the upper limit of the normal serum range.

Words asserted that the passage nowhere uses: `above, thresholds`

*Why flagged:* the explanation attributes a definition or statement to the passage; compare the two sentences and decide whether the attribution is accurate

**Ruling** (clean / defective / edge): ______  **By:** ______  **Note:** ______


---

## 23. vd-clean-001  ·  Physiology

`label: CLEAN` · `label_status: unreviewed` · `reviewers: 0`

**Passage.** Cardiac output is the volume of blood ejected by one ventricle each minute. It is the product of stroke volume, the volume ejected per beat, and heart rate, the number of beats per minute. Stroke volume is itself the difference between end-diastolic and end-systolic volume.

**Explanation.** The passage defines cardiac output as the volume ejected per beat (stroke volume) multiplied by the number of beats per minute. Option 4 defines stroke volume itself, not cardiac output.

**Claim 1 — `attribution`**

> The passage defines cardiac output as the volume ejected per beat (stroke volume) multiplied by the number of beats per minute.

Nearest passage sentence:

> It is the product of stroke volume, the volume ejected per beat, and heart rate, the number of beats per minute.

Words asserted that the passage nowhere uses: `multiplied`

*Why flagged:* the explanation attributes a definition or statement to the passage; compare the two sentences and decide whether the attribution is accurate

**Claim 2 — `options`**

> Option 4 defines stroke volume itself, not cardiac output.

*Why flagged:* the explanation makes a claim about numbered options; the passage never refers to options by number, so a checker comparing the two has nothing to match against

**Ruling** (clean / defective / edge): ______  **By:** ______  **Note:** ______


---

## 24. vd-clean-024  ·  Medicine

`label: CLEAN` · `label_status: unreviewed` · `reviewers: 0`

**Passage.** The diagnosis of chronic obstructive pulmonary disease requires demonstration of persistent airflow limitation, defined spirometrically as a ratio of forced expiratory volume in one second to forced vital capacity below 0.70 measured after a bronchodilator. Measuring before bronchodilator cannot distinguish fixed from reversible obstruction. Marked reversibility of FEV1 favours asthma. A reduced total lung capacity with a preserved ratio indicates a restrictive rather than an obstructive defect.

**Explanation.** The passage specifies that the ratio must be measured after bronchodilator and must be below 0.70. Option 3 describes significant reversibility, which the passage associates with asthma, and option 4 describes a restrictive pattern.

**Claim 1 — `attribution`**

> The passage specifies that the ratio must be measured after bronchodilator and must be below 0.70.

Nearest passage sentence:

> The diagnosis of chronic obstructive pulmonary disease requires demonstration of persistent airflow limitation, defined spirometrically as a ratio of forced expiratory volume in one second to forced vital capacity below 0.70 measured after a bronchodilator.

Words asserted that the passage nowhere uses: `must`

*Why flagged:* the explanation attributes a definition or statement to the passage; compare the two sentences and decide whether the attribution is accurate

**Claim 2 — `options`**

> Option 3 describes significant reversibility, which the passage associates with asthma, and option 4 describes a restrictive pattern.

*Why flagged:* the explanation makes a claim about numbered options; the passage never refers to options by number, so a checker comparing the two has nothing to match against

**Ruling** (clean / defective / edge): ______  **By:** ______  **Note:** ______


---

## 25. vd-clean-031  ·  Obstetrics and Gynaecology

`label: CLEAN` · `label_status: unreviewed` · `reviewers: 0`

**Passage.** Primary postpartum haemorrhage is bleeding of 500 millilitres or more from the genital tract within 24 hours of delivery. Its causes are conventionally grouped as tone, trauma, tissue and thrombin. Failure of the uterus to contract, that is uterine atony, accounts for the large majority of cases. Genital tract trauma, retained placental tissue and coagulation disorders account for the remainder.

**Explanation.** The passage states that atony accounts for the large majority of primary postpartum haemorrhage, with trauma, retained tissue and coagulopathy accounting for the remainder.

**Claim 1 — `attribution`**

> The passage states that atony accounts for the large majority of primary postpartum haemorrhage, with trauma, retained tissue and coagulopathy accounting for the remainder.

Nearest passage sentence:

> Genital tract trauma, retained placental tissue and coagulation disorders account for the remainder.

Words asserted that the passage nowhere uses: `coagulopathy`

*Why flagged:* the explanation attributes a definition or statement to the passage; compare the two sentences and decide whether the attribution is accurate

**Ruling** (clean / defective / edge): ______  **By:** ______  **Note:** ______


---

## 26. vd-clean-037  ·  PSM

`label: CLEAN` · `label_status: unreviewed` · `reviewers: 0`

**Passage.** Incidence is the number of new cases of a condition arising in a defined population at risk over a specified period, and measures the rate at which people acquire the disease. Prevalence is the number of existing cases, new and old, present in a defined population at a point in time, and measures the burden being carried. A chronic disease of long duration can have a low incidence and a high prevalence; a brief self-limiting illness can show the opposite.

**Explanation.** The passage defines incidence as new cases per population at risk over a period and prevalence as existing cases at a point in time. It also states that a long-lasting disease can have high prevalence with low incidence, contradicting option 4.

**Claim 1 — `attribution`**

> The passage defines incidence as new cases per population at risk over a period and prevalence as existing cases at a point in time.

Nearest passage sentence:

> Prevalence is the number of existing cases, new and old, present in a defined population at a point in time, and measures the burden being carried.

Words asserted that the passage nowhere uses: `per`

*Why flagged:* the explanation attributes a definition or statement to the passage; compare the two sentences and decide whether the attribution is accurate

**Claim 2 — `options`**

> It also states that a long-lasting disease can have high prevalence with low incidence, contradicting option 4.

*Why flagged:* the explanation makes a claim about numbered options; the passage never refers to options by number, so a checker comparing the two has nothing to match against

**Ruling** (clean / defective / edge): ______  **By:** ______  **Note:** ______


---

## 27. vd-clean-010  ·  Pathology

`label: CLEAN` · `label_status: unreviewed` · `reviewers: 0`

**Passage.** In apoptosis the cell shrinks, chromatin condenses, and the cell fragments into membrane-bound apoptotic bodies that are phagocytosed intact. Because cell contents are never spilled into the interstitium, there is no inflammatory reaction. The process is energy-dependent and typically affects scattered single cells. Necrosis, by contrast, involves early membrane failure, leakage of contents, an inflammatory infiltrate, and usually affects contiguous groups of cells.

**Explanation.** The passage contrasts the two on membrane integrity and inflammation, and separately notes that apoptosis is ATP-dependent and affects single cells. Options 2 to 4 each state the necrotic pattern or the opposite of the passage.

**Claim 1 — `options`**

> Options 2 to 4 each state the necrotic pattern or the opposite of the passage.

*Why flagged:* the explanation makes a claim about numbered options; the passage never refers to options by number, so a checker comparing the two has nothing to match against

**Ruling** (clean / defective / edge): ______  **By:** ______  **Note:** ______


---

## 28. vd-clean-014  ·  Pharmacology

`label: CLEAN` · `label_status: unreviewed` · `reviewers: 0`

**Passage.** Aspirin acetylates a serine residue in the active site of cyclo-oxygenase, inactivating the enzyme irreversibly. The plasma half-life of aspirin itself is only a few minutes. Because the platelet is anucleate and cannot synthesise replacement enzyme, thromboxane A2 production does not recover in that platelet, and platelet function returns only as new platelets enter the circulation over the following days.

**Explanation.** The passage attributes the long effect to irreversible acetylation combined with the anucleate platelet's inability to make new enzyme, and explicitly notes that the plasma half-life is short. Option 4 describes the mechanism of the thienopyridines.

**Claim 1 — `options`**

> Option 4 describes the mechanism of the thienopyridines.

*Why flagged:* the explanation makes a claim about numbered options; the passage never refers to options by number, so a checker comparing the two has nothing to match against

**Ruling** (clean / defective / edge): ______  **By:** ______  **Note:** ______


---
