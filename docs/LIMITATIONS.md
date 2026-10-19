# Limitations and threats to validity

## External validity
The v2 final result is synthetic. It does not establish the same boundary on production traces, FEMU/WARP, or commercial SSDs.

## Model simplification
The simulator does not faithfully model all channels/dies, controller CPU, DRAM contention, firmware scheduling, NVMe queues, power failure, or hardware-specific GC.

## Binary symmetry
The `|phi|` argument relies on a two-group setting where full label inversion can remain physically equivalent. Real placement resources may be asymmetric.

## Device-only baseline
The Device-only baseline is intentionally transparent rather than state of the art. Beating it is informative for the controlled question but not sufficient to beat modern lifetime-prediction research.

## Delayed/censored evidence
Lifetime is only resolved after overwrite. Long-lived data naturally provides evidence later, creating delayed and potentially composition-biased observations.

## Dependence
Non-overlapping evidence blocks prevent sample reuse but do **not** imply statistical independence. Repeated LPN activity and workload phases create dependence.

## Small final seed count
Five final stochastic seeds are reasonable for this scoped proof-of-concept but not strong population-level evidence.

## Sticky one-way fallback
The final policy never re-enters Host-guided mode. Recovery/hysteresis remains untested.

## Wear trade-off
Lower WAF can coexist with worse erase-count balance. Long-run endurance was not optimized.

## Boundary specificity
The calibrated `|phi|` threshold and B/K are workload/model dependent, not universal constants.
