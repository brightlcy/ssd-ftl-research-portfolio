# Related-work positioning and novelty guardrails

This project should **not** claim novelty for broad ideas that already exist in storage research.

## Areas already well explored

- Host/application stream separation
- program-context-based stream classification
- lifetime/invalidation-time prediction
- adaptive grouping
- limited stream/group resources
- multi-tenant SSD isolation
- FDP misclassification/interference analysis

Relevant families already studied include FStream, PCStream, SepBIT, MIDAS, Valet, WARP, DOGI, vStream, and FlashBlox.

## Narrow contribution framing

The strongest current framing is:

> Given an already-existing imperfect Host Hint, characterize the empirical crossover where trusting it becomes worse than a transparent Device-only baseline, then test whether online completed-lifetime evidence can conservatively trigger fallback and limit WAF regret.

This is a **combination of problem framing + empirical boundary + fallback behavior**, not a claim that host-guided placement or lifetime prediction itself is new.

## Reviewer-safe wording

Use:
- “FDP-inspired Host-guided placement simulation”
- “controlled synthetic FTL experiment”
- “empirical failure boundary”
- “persistence-aware fallback”
- “proof-of-concept mechanism evidence”

Avoid:
- “new FDP algorithm”
- “first safe fallback for SSDs”
- “industry-ready”
- “universally optimal”
