from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from simulator.experiment_runner import run_phased_baseline_experiment
    from simulator.hot_cold_experiment import run_phased_hot_cold_experiment
else:
    from .experiment_runner import run_phased_baseline_experiment
    from .hot_cold_experiment import run_phased_hot_cold_experiment


def _safe_percent_change(baseline: float, proposed: float) -> float:
    if baseline == 0:
        return 0.0 if proposed == 0 else math.inf
    return ((proposed - baseline) / baseline) * 100.0


def _extract(result: Any) -> Dict[str, float]:
    summary = result.summary
    latency = result.latency_summary["all"]
    causes = result.latency_cause_summary
    tail = result.tail_attribution_summary["p99"]
    gc_count = float(summary["gc_count"])
    return {
        "write_amplification_factor": float(summary["write_amplification_factor"]),
        "valid_page_migrations": float(summary["valid_page_migrations"]),
        "gc_count": gc_count,
        "avg_migrated_pages_per_gc": (
            float(summary["valid_page_migrations"]) / gc_count if gc_count else 0.0
        ),
        "avg_latency_us": float(latency["avg_latency_us"]),
        "p95_latency_us": float(latency["p95_latency_us"]),
        "p99_latency_us": float(latency["p99_latency_us"]),
        "p99_9_latency_us": float(latency["p99_9_latency_us"]),
        "max_latency_us": float(latency["max_latency_us"]),
        "direct_gc_requests": float(causes["direct_gc"]["request_count"]),
        "gc_spillover_requests": float(causes["gc_spillover"]["request_count"]),
        "p99_gc_related_ratio": float(tail["gc_related_ratio"]),
    }


def _write_comparison(path: Path, baseline: Dict[str, float], proposed: Dict[str, float]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "metric",
                "baseline",
                "hot_cold",
                "absolute_change",
                "percent_change",
            ),
        )
        writer.writeheader()
        for metric in baseline:
            base = baseline[metric]
            prop = proposed[metric]
            writer.writerow(
                {
                    "metric": metric,
                    "baseline": base,
                    "hot_cold": prop,
                    "absolute_change": prop - base,
                    "percent_change": _safe_percent_change(base, prop),
                }
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run equal-condition Baseline vs Online Hot/Cold comparison."
    )
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--warmup-rounds", type=int, default=1)
    parser.add_argument("--measurement-rounds", type=int, default=1)
    parser.add_argument("--page-size", type=int, default=4096)
    parser.add_argument("--chips", type=int, default=4)
    parser.add_argument("--pages-per-block", type=int, default=64)
    parser.add_argument("--overprovision-ratio", type=float, default=0.20)
    parser.add_argument("--gc-threshold-ratio", type=float, default=0.05)
    parser.add_argument("--reserved-gc-blocks", type=int, default=2)
    parser.add_argument("--capacity-basis", default="write-working-set")
    parser.add_argument("--hot-threshold", type=int, default=2)
    parser.add_argument("--hot-window-writes", type=int, default=4096)
    parser.add_argument("--debug-check", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.output_dir).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    common = dict(
        warmup_rounds=args.warmup_rounds,
        measurement_rounds=args.measurement_rounds,
        page_size=args.page_size,
        chips=args.chips,
        pages_per_block=args.pages_per_block,
        overprovision_ratio=args.overprovision_ratio,
        gc_threshold_ratio=args.gc_threshold_ratio,
        reserved_gc_blocks=args.reserved_gc_blocks,
        capacity_basis=args.capacity_basis,
        debug_check=args.debug_check,
    )
    try:
        baseline_result = run_phased_baseline_experiment(
            args.input,
            root / "baseline",
            experiment_name="baseline-greedy",
            **common,
        )
        proposed_result = run_phased_hot_cold_experiment(
            args.input,
            root / "hot_cold",
            experiment_name="online-hot-cold",
            hot_threshold=args.hot_threshold,
            hot_window_writes=args.hot_window_writes,
            **common,
        )
    except Exception as exc:
        print(f"Policy comparison failed: {exc}", file=sys.stderr)
        return 1

    baseline = _extract(baseline_result)
    proposed = _extract(proposed_result)
    comparison_path = root / "policy_comparison.csv"
    _write_comparison(comparison_path, baseline, proposed)
    payload = {
        "baseline": baseline,
        "hot_cold": proposed,
        "hot_cold_policy": {
            "hot_threshold": args.hot_threshold,
            "hot_window_writes": args.hot_window_writes,
            "future_information_used": False,
        },
        "artifacts": {
            "baseline_dir": "baseline",
            "hot_cold_dir": "hot_cold",
            "comparison": comparison_path.name,
        },
    }
    manifest_path = root / "comparison_manifest.json"
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    print("Baseline vs Online Hot/Cold comparison completed")
    for metric in (
        "write_amplification_factor",
        "valid_page_migrations",
        "gc_count",
        "p99_latency_us",
        "p99_9_latency_us",
        "gc_spillover_requests",
    ):
        change = _safe_percent_change(baseline[metric], proposed[metric])
        print(
            f"  {metric}: baseline={baseline[metric]:.6f} "
            f"hot_cold={proposed[metric]:.6f} change={change:.3f}%"
        )
    print(f"  comparison: {comparison_path}")
    print(f"  manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
