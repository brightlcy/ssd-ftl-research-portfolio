# SSD/FTL Research Portfolio

Two independently reproducible research projects, connected by a common FTL implementation lineage.

| Project | Research question | Main observation and boundary |
|---|---|---|
| [Online Hot/Cold FTL](https://github.com/brightlcy/online-hotcold-ftl) | When does recent-write placement reduce greedy GC copying? | Alibaba dev4 slice: WAF −34.03%, migration −47.51%; dev740 is neutral. Four finite trace slices, Python model. |
| [Host Hint Safe Fallback](https://github.com/brightlcy/host-hint-safe-fallback) | When should imperfect host placement hints stop being trusted? | Seed-equal false fallback 34.67% → 8.00%; harmful WAF improves with worse erase-count balance. Five synthetic final seeds. |

Each project contains its own source, tests, configs, archived evidence, figure generator, provenance manifests and methodology. Both remain **Private**. Follow each repository's README for verified execution status and limitations.

## How the studies connect

Online Hot/Cold uses past write frequency to separate placement on real trace extracts. Host Hint inherits the FTL base and studies imperfect lifetime labels in a controlled synthetic environment. The Host Hint final path does not replay Alibaba or invoke HotColdFTL. Their WAF evaluation intervals differ, so magnitudes should not be compared as one experiment.

The frozen shared source is intentionally preserved in both repositories. Extracting a new live core dependency would change the closed experiments' imports and provenance; a maintained shared package belongs to a separately versioned future effort.

## What was verified in the rebuild

- Source and original result archives are preserved byte-for-byte.
- Independent project execution, source/result checks, smoke runs and optimized-mode plot validators are provided.
- Host Hint production historical replay reproduced 220 policy-run rows and all final scientific output files byte-for-byte using restored original Git history and unchanged guards. This reuses known final seeds; it is not a new untouched holdout.
- Alibaba dev4/dev49 paired replays and dev740 Baseline match. The long dev740 Hot/Cold replay is still pending; the MSR input is absent from the export. No rerun success is claimed for those cases.
- Five new figures include neutral behavior, remaining false fallbacks, label symmetry and wear imbalance. WAF is page programs / host writes, not pages moved per GC.

These are simulator studies. They do not establish actual SSD latency/endurance, a real NVMe FDP implementation, or independent human authorship of every code line. Contribution and AI-assistance boundaries are explicit in each project.

## Preserved earlier material

This repository keeps the original documentation, figures and partial source upload for provenance. Its earlier README is [retained verbatim](docs/legacy/README_20261020_before_rebuild.md); the pre-rebuild commit is [213e748](https://github.com/brightlcy/ssd-ftl-research-portfolio/commit/213e74847edcb292d2c8a4cb8af11c25e5379606). Old descriptions and old figures can be superseded by the two projects' current methodology and limitations.

MQSim chip-isolation configs/results remain in the separately preserved local research export and archive. A modified MQSim engine source/build was not recovered, so that work is not presented here as a fully reproducible third code project.
