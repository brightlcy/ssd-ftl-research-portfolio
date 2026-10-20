from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from simulator.hot_cold_experiment import run_phased_hot_cold_experiment
    from simulator.trace_io import (
        CAPACITY_BASIS_CHOICES,
        CAPACITY_BASIS_WRITE_WORKING_SET,
    )
else:
    from .hot_cold_experiment import run_phased_hot_cold_experiment
    from .trace_io import (
        CAPACITY_BASIS_CHOICES,
        CAPACITY_BASIS_WRITE_WORKING_SET,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run online hot/cold placement with the baseline GC policy."
    )
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--experiment-name", default=None)
    parser.add_argument("--warmup-rounds", type=int, default=0)
    parser.add_argument("--measurement-rounds", type=int, default=1)
    parser.add_argument("--page-size", type=int, default=4096)
    parser.add_argument("--chips", type=int, default=4)
    parser.add_argument("--pages-per-block", type=int, default=64)
    parser.add_argument("--overprovision-ratio", type=float, default=0.20)
    parser.add_argument("--gc-threshold-ratio", type=float, default=0.05)
    parser.add_argument("--reserved-gc-blocks", type=int, default=2)
    parser.add_argument(
        "--capacity-basis",
        choices=CAPACITY_BASIS_CHOICES,
        default=CAPACITY_BASIS_WRITE_WORKING_SET,
    )
    parser.add_argument(
        "--hot-threshold",
        type=int,
        default=2,
        help="recent writes required for an LPN to become hot",
    )
    parser.add_argument(
        "--hot-window-writes",
        type=int,
        default=4096,
        help="global host-page-write window used by online hotness tracking",
    )
    parser.add_argument("--command-overhead-us", type=float, default=20.0)
    parser.add_argument("--read-page-us", type=float, default=80.0)
    parser.add_argument("--program-page-us", type=float, default=200.0)
    parser.add_argument("--erase-block-us", type=float, default=2000.0)
    parser.add_argument("--debug-check", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run_phased_hot_cold_experiment(
            args.input,
            args.output_dir,
            warmup_rounds=args.warmup_rounds,
            measurement_rounds=args.measurement_rounds,
            page_size=args.page_size,
            chips=args.chips,
            pages_per_block=args.pages_per_block,
            overprovision_ratio=args.overprovision_ratio,
            gc_threshold_ratio=args.gc_threshold_ratio,
            reserved_gc_blocks=args.reserved_gc_blocks,
            capacity_basis=args.capacity_basis,
            hot_threshold=args.hot_threshold,
            hot_window_writes=args.hot_window_writes,
            command_overhead_us=args.command_overhead_us,
            read_page_us=args.read_page_us,
            program_page_us=args.program_page_us,
            erase_block_us=args.erase_block_us,
            debug_check=args.debug_check,
            experiment_name=args.experiment_name,
        )
    except Exception as exc:
        print(f"Hot/Cold FTL run failed: {exc}", file=sys.stderr)
        return 1

    summary = result.summary
    all_latency = result.latency_summary["all"]
    causes = result.latency_cause_summary
    print("Online Hot/Cold FTL run completed")
    print(f"  host_write_pages: {summary['host_write_pages']}")
    print(f"  hot_host_write_pages: {summary['hot_host_write_pages']}")
    print(f"  cold_host_write_pages: {summary['cold_host_write_pages']}")
    print(f"  nand_write_pages: {summary['nand_write_pages']}")
    print(f"  valid_page_migrations: {summary['valid_page_migrations']}")
    print(f"  gc_count: {summary['gc_count']}")
    print(f"  WAF: {summary['write_amplification_factor']:.6f}")
    print(f"  p99_latency_us: {all_latency['p99_latency_us']:.3f}")
    print(f"  p99_9_latency_us: {all_latency['p99_9_latency_us']:.3f}")
    print(f"  direct_gc_requests: {causes['direct_gc']['request_count']}")
    print(f"  gc_spillover_requests: {causes['gc_spillover']['request_count']}")
    print(f"  placement_fallback_pages: {summary['placement_fallback_pages']}")
    print(f"  summary: {result.summary_path}")
    print(f"  manifest: {result.manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
