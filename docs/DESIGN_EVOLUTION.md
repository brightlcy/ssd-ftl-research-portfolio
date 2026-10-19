# Design Evolution

## Stage 1 — Initial hypothesis

**Hypothesis:** More semantic Host Hint error should monotonically worsen Static Host-guided placement.

### Counterexample
At 100% SHORT/LONG inversion, performance recovered.

### Interpretation
With symmetric binary placement groups, full inversion preserves segregation and only swaps group names.

### Revision
Semantic accuracy was demoted. Lifetime-separation power became the core variable.

---

## Stage 2 — v1 rolling detector

The first fallback detector used rolling association evidence.

### Positive result
It reduced WAF regret when Static Host-guided placement was truly harmful.

### Negative result
It also triggered in beneficial regions because early/transient association dips caused a sticky one-way fallback.

### Decision
Do not tune v1 on its held-out seeds. Freeze the failure as evidence and design a new detector.

---

## Stage 3 — v2 persistence detector

The redesign required bad association to persist across non-overlapping evidence blocks.

Goal:
- preserve harmful-regime detection,
- lower false fallback.

Candidate B/K values were compared on new development seeds.

---

## Stage 4 — Final holdout

The selected configuration was frozen and evaluated once on untouched final seeds.

No final-result-driven retuning is permitted.

---

## Why this design story matters

The project is stronger when presented as:

> “I repeatedly found ways my own assumptions were wrong and changed the model only when the evidence justified it.”

It is weaker when presented as:

> “I invented a detector that improved WAF.”
