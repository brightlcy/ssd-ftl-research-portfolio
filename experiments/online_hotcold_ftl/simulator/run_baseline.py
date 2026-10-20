from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence, Union

# Support both recommended module execution
#   python3 -m simulator.run_baseline ...
# and direct-file execution
#   python3 simulator/run_baseline.py ...
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from simulator.experiment_runner import (
        PhasedBaselineRunResult,
        run_phased_baseline_experiment,
    )
    from simulator.trace_io import (
        CAPACITY_BASIS_CHOICES,
        CAPACITY_BASIS_WRITE_WORKING_SET,
    )
else:
    from .experiment_runner import (
        PhasedBaselineRunResult,
        run_phased_baseline_experiment,
    )
    from .trace_io import (
        CAPACITY_BASIS_CHOICES,
        CAPACITY_BASIS_WRITE_WORKING_SET,
    )


PathLike = Union[str, Path]
BaselineRunResult = PhasedBaselineRunResult



def run_baseline_experiment(
    input_path: PathLike,
    output_dir: PathLike,
    *,
    warmup_rounds: int = 0,
    measurement_rounds: int = 1,
    page_size: int = 4096,
    chips: int = 4,
    pages_per_block: int = 64,
    overprovision_ratio: float = 0.20,
    gc_threshold_ratio: float = 0.05,
    reserved_gc_blocks: int = 2,
    debug_check: bool = False,
    capacity_basis: str = CAPACITY_BASIS_WRITE_WORKING_SET,
    experiment_name: Optional[str] = None,
    command_overhead_us: float = 20.0,
    read_page_us: float = 80.0,
    program_page_us: float = 200.0,
    erase_block_us: float = 2000.0,
) -> BaselineRunResult:
    """Compatibility wrapper around the phased experiment runner."""
    return run_phased_baseline_experiment(
        input_path,
        output_dir,
        warmup_rounds=warmup_rounds,
        measurement_rounds=measurement_rounds,
        page_size=page_size,
        chips=chips,
        pages_per_block=pages_per_block,
        overprovision_ratio=overprovision_ratio,
        gc_threshold_ratio=gc_threshold_ratio,
        reserved_gc_blocks=reserved_gc_blocks,
        debug_check=debug_check,
        capacity_basis=capacity_basis,
        experiment_name=experiment_name,
        command_overhead_us=command_overhead_us,
        read_page_us=read_page_us,
        program_page_us=program_page_us,
        erase_block_us=erase_block_us,
    )



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the deterministic page-mapping Baseline FTL with optional "
            "warm-up and a single-resource analytical latency model."
        )
    )
    parser.add_argument(
        "--input",
        required=True,
        help="standard trace CSV: timestamp,flow_id,op,lba,size",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="directory for summaries, GC logs, latency logs, config, and manifest",
    )
    parser.add_argument(
        "--experiment-name",
        default=None,
        help="human-readable run name stored in experiment_manifest.json",
    )
    parser.add_argument(
        "--warmup-rounds",
        type=int,
        default=0,
        help="trace replays before measurement; SSD state is preserved",
    )
    parser.add_argument(
        "--measurement-rounds",
        type=int,
        default=1,
        help="trace replays included in baseline and latency summaries",
    )
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
        help=(
            "SSD sizing basis: distinct write working set (recommended) or "
            "raw max-LPN address span"
        ),
    )
    parser.add_argument(
        "--command-overhead-us",
        type=float,
        default=20.0,
        help="fixed modeled controller cost per request",
    )
    parser.add_argument(
        "--read-page-us",
        type=float,
        default=80.0,
        help="modeled NAND read cost per page",
    )
    parser.add_argument(
        "--program-page-us",
        type=float,
        default=200.0,
        help="modeled NAND program cost per page",
    )
    parser.add_argument(
        "--erase-block-us",
        type=float,
        default=2000.0,
        help="modeled NAND block erase cost per GC cycle",
    )
    parser.add_argument(
        "--debug-check",
        action="store_true",
        help="verify metadata/accounting invariants after writes and GC cycles",
    )
    return parser



def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)



def _print_completion(result: BaselineRunResult) -> None:
    summary = result.summary
    all_latency = result.latency_summary["all"]
    write_latency = result.latency_summary["write"]
    direct_gc = result.latency_cause_summary["direct_gc"]
    gc_spillover = result.latency_cause_summary["gc_spillover"]
    p99_tail = result.tail_attribution_summary["p99"]
    print("Baseline FTL run completed (phased experiment + latency model)")
    print(f"  capacity_basis: {result.config.capacity_basis}")
    print(f"  logical_capacity_pages: {result.config.logical_pages}")
    print(f"  physical_pages: {result.config.total_pages}")
    print(f"  measurement_host_write_pages: {summary['host_write_pages']}")
    print(f"  measurement_nand_write_pages: {summary['nand_write_pages']}")
    print(
        "  measurement_valid_page_migrations: "
        f"{summary['valid_page_migrations']}"
    )
    print(f"  measurement_gc_count: {summary['gc_count']}")
    print(
        "  measurement_write_amplification_factor: "
        f"{summary['write_amplification_factor']:.6f}"
    )
    print(f"  modeled_avg_latency_us: {all_latency['avg_latency_us']:.3f}")
    print(f"  modeled_p95_latency_us: {all_latency['p95_latency_us']:.3f}")
    print(f"  modeled_p99_latency_us: {all_latency['p99_latency_us']:.3f}")
    print(f"  modeled_p99_9_latency_us: {all_latency['p99_9_latency_us']:.3f}")
    print(f"  modeled_max_latency_us: {all_latency['max_latency_us']:.3f}")
    print(
        "  gc_affected_write_requests: "
        f"{write_latency['gc_affected_requests']}"
    )
    print(f"  direct_gc_requests: {direct_gc['request_count']}")
    print(f"  gc_spillover_requests: {gc_spillover['request_count']}")
    print(
        "  p99_gc_related_ratio: "
        f"{p99_tail['gc_related_ratio']:.6f}"
    )
    print("  timing_note: analytical single-resource model; not hardware calibrated")
    print(f"  warmup_summary: {result.warmup_summary_path}")
    print(f"  measurement_summary: {result.summary_path}")
    print(f"  cumulative_summary: {result.cumulative_summary_path}")
    print(f"  warmup_gc_log: {result.warmup_gc_log_path}")
    print(f"  measurement_gc_log: {result.gc_log_path}")
    print(f"  request_latency: {result.request_latency_path}")
    print(f"  latency_summary: {result.latency_summary_path}")
    print(f"  latency_attribution: {result.latency_attribution_path}")
    print(f"  latency_cause_summary: {result.latency_cause_summary_path}")
    print(f"  tail_attribution_summary: {result.tail_attribution_summary_path}")
    print(f"  resolved_config: {result.config_path}")
    print(f"  manifest: {result.manifest_path}")



def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        result = run_baseline_experiment(
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
            debug_check=args.debug_check,
            capacity_basis=args.capacity_basis,
            experiment_name=args.experiment_name,
            command_overhead_us=args.command_overhead_us,
            read_page_us=args.read_page_us,
            program_page_us=args.program_page_us,
            erase_block_us=args.erase_block_us,
        )
    except Exception as exc:
        print(f"Baseline FTL run failed: {exc}", file=sys.stderr)
        return 1

    _print_completion(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
