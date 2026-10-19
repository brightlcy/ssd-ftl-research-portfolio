# Reproducibility checklist

## Frozen facts

- protocol commit: `72eb2b472ac9a7b385bca47b106d58ba6d909d53`
- selection commit: `f4fc53f951abf0ad9f9451171a0ac4fb06fc76ea`
- final execution commit: `c27b72ae76766a05b6c83de89907ffb00bdf1e71`
- final seeds: `[301, 302, 303, 304, 305]`
- B/K: `512/2`
- lifetime boundary: `451`
- association threshold: `0.29980003993028376`
- error grid: `[0.0, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5, 0.6, 0.8, 1.0]`

## Required invariants

- Same `(seed,error-rate)` workload/hint realization across Static/v1/v2.
- Device-only and zero-information baseline are error-rate independent.
- Final seeds must not enter calibration/development.
- Any new experiments after final closeout are extensions, not additional tuning of the original core claim.
- Dirty worktree production runs should be rejected or clearly marked non-reproducible.
