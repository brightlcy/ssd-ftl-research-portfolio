# Failure Boundary and Persistence-Aware Fallback for Imperfect Host-Guided SSD Placement

> When should an SSD stop trusting host-provided placement hints?

## Draft status

This is a portfolio documentation draft for review and later revision.
The current snapshot contains the README, research documents, and four figures.
Simulator code, tests, raw results, and personal study/interview materials are
maintained separately and are not included in this snapshot.

## Overview

This project studies a failure mode of Host-guided SSD data placement: a host hint can be useful when it preserves data-lifetime separation, but harmful when that separation collapses.

Rather than proposing a new lifetime predictor, the project asks:

1. When does Static Host-guided placement become worse than a Device-only policy?
2. Can that failure region be detected online using only resolved past lifetimes?
3. Can a conservative fallback reduce worst-case WAF regret without excessive false fallback?

## Key counterexample

The initial hypothesis was that more binary hint errors would monotonically worsen performance.

It was false.

With two symmetric placement groups, a 100% SHORT/LONG inversion can preserve perfect segregation while only swapping group names. This shifted the core variable from **semantic accuracy** to **lifetime-separation association**.

The project therefore uses label-invariant `|phi|` association between Host Hint class and online-resolved lifetime class as the observed separation signal.

## Evolution

```text
Semantic error hypothesis
        ↓ counterexample
Lifetime-separation hypothesis
        ↓
v1 rolling fallback
        ↓ false fallback under transient dips
Persistence-aware v2
        ↓
Development freeze
        ↓
Untouched final holdout
```

## Final protocol

| Item | Value |
|---|---:|
| Lifetime boundary | `451` host writes |
| Association threshold | `0.2998000399` |
| v2 block size | `512` usable resolved pairs |
| Persistence | `2` consecutive BAD blocks |
| Final seeds | `301–305` |
| Writes/run | `50,000` |
| Logical pages | `2,048` |

## Final result

**FINAL_SUPPORT**

- Harmful conditions detected timely: **21/21**
- Harmful conditions with lower WAF than Static: **21/21**
- Beneficial false fallback: **v1 17/49 → v2 4/49**
- Seed-equal beneficial false-fallback rate: **34.7% → 8.0%**

## What did not improve

Wear imbalance increased in the harmful final conditions even when WAF/NAND writes/migration/GC improved.

This is reported as a trade-off, not hidden as a secondary detail.

## Scope

This repository demonstrates a **controlled synthetic FTL experiment**.

It is **not**:
- a real NVMe FDP implementation,
- a universal threshold study,
- a real SSD latency benchmark,
- proof that B=512/K=2 is optimal across workloads.

## Planned full research repository structure

The structure below is a plan for a future code-inclusive version, not a list of
files included in this documentation snapshot.

```text
.
├── README.md
├── simulator/
│   ├── baseline_ftl.py
│   ├── lifetime_ftl.py
│   ├── lifetime_tracker.py
│   ├── persistence_detector.py
│   └── persistence_protocol.py
├── tests/
├── docs/
│   ├── METHODOLOGY.md
│   ├── DESIGN_EVOLUTION.md
│   ├── RELATED_WORK_POSITIONING.md
│   ├── LIMITATIONS.md
│   └── REPRODUCIBILITY.md
├── figures/
└── results/
    └── final_summary/
```

## Best way to read this repository

1. `docs/DESIGN_EVOLUTION.md`
2. `docs/METHODOLOGY.md`
3. `figures/`
4. `docs/LIMITATIONS.md`
5. code/tests, when a reviewed code snapshot is added

The most important result is the reasoning path, not a single percentage.

## Editing this draft

Edit `README.md` and `docs/` as the portfolio develops. Figures are in `figures/`.
The GitHub file editor can be used for small text changes.

For local changes, review and commit only the upload repository contents:

```sh
git status
git diff
git add README.md docs figures .gitignore
git diff --cached --stat
git commit -m "Update portfolio documentation"
git push origin main
```

Local pushes require GitHub authentication on the computer. Keep personal study
materials, answer keys, historical archives, and raw datasets outside this repository.
