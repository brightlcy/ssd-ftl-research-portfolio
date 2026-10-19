# Methodology

## Research question

The primary evaluation asks whether loss of Host Hint lifetime-separation power creates a region where Static Host-guided placement becomes worse than a Device-only lifetime baseline, and whether online fallback can limit that degradation.

## Ground-truth lifetime

A page-version is born on a host page write and dies when the same `(namespace_id, LPN)` is overwritten.

Primary lifetime:
`host-write distance = death write ordinal - birth write ordinal`.

Right-censored versions are not treated as exact long lifetimes unless their observed age is already sufficient to establish the binary class.

## Evaluation information boundary

- Online policies: past/current information only.
- Future death time: prohibited.
- Oracle/offline analysis: may use future information.
- GC migration does not create host-write lifetime observations.

## Final v2 detector

Each resolved lifetime contributes a binary `(true lifetime class, host hint class)` pair.

For each **non-overlapping** block of `512` usable pairs:

1. compute binary phi association,
2. take `|phi|` for label-invariance,
3. mark block BAD if `|phi| < 0.2998000399`,
4. fallback after `2` consecutive BAD blocks,
5. GOOD resets the streak,
6. invalid/degenerate evidence is not treated as good evidence.

The policy is one-way: after fallback, new host writes use the Device-side placement baseline.

## Why `|phi|`

For binary labels:

- perfect correct labeling → `phi=+1`
- random mixing → `phi≈0`
- perfect inversion → `phi=-1`

If physical placement groups are symmetric, both `+1` and `-1` preserve segregation. Therefore `|phi|` captures separation power better than semantic correctness in this model.

## Experimental design

- same workload and same corrupted hint realization within a `(seed,error-rate)` condition,
- paired comparison across policies,
- deterministic reruns are not counted as independent samples,
- calibration/development/final seeds are separated,
- final seeds are not used for tuning.

## Primary and secondary metrics

Primary:
- WAF
- valid-page migration

Secondary:
- NAND write pages
- GC count
- false fallback
- trigger timing
- wear imbalance CV

## Final selection rule

B/K candidate selection happened only on development seeds, using predeclared seed-level rules. The selected B/K was frozen before final evaluation.

Final verdict was also based on gates defined before reading final results.
