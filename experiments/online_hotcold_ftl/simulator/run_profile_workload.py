from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from simulator.workload_profile import profile_standard_trace
else:
    from .workload_profile import profile_standard_trace


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Profile a standard FTL trace for burst, locality, and overwrite signals."
    )
    parser.add_argument("--input", required=True, help="standard trace CSV")
    parser.add_argument("--output-dir", required=True, help="artifact directory")
    parser.add_argument(
        "--window-seconds",
        type=float,
        default=1.0,
        help="fixed analysis window width in seconds",
    )
    parser.add_argument(
        "--burst-percentile",
        type=float,
        default=95.0,
        help="percentile of non-empty window request counts used as burst threshold",
    )
    parser.add_argument(
        "--top-hot-lpn-count",
        type=int,
        default=100,
        help="number of hottest written LPNs to save",
    )
    parser.add_argument(
        "--max-tracked-write-pages",
        type=int,
        default=5_000_000,
        help="safety cap for exact page-level overwrite/hotness tracking",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = profile_standard_trace(
            args.input,
            args.output_dir,
            window_seconds=args.window_seconds,
            burst_percentile=args.burst_percentile,
            top_hot_lpn_count=args.top_hot_lpn_count,
            max_tracked_write_pages=args.max_tracked_write_pages,
        )
    except Exception as exc:
        print(f"Workload profiling failed: {exc}", file=sys.stderr)
        return 1

    summary = result.summary
    print("Workload profiling completed")
    print(f"  total_requests: {summary['total_requests']}")
    print(f"  write_request_ratio: {summary['write_request_ratio']:.6f}")
    print(f"  write_page_ratio: {summary['write_page_ratio']:.6f}")
    print(f"  p95_request_size_pages: {summary['p95_request_size_pages']:.3f}")
    print(f"  sequential_transition_ratio: {summary['sequential_transition_ratio']:.6f}")
    print(f"  overwrite_page_ratio: {summary['overwrite_page_ratio']:.6f}")
    print(f"  burst_iops_threshold: {summary['burst_iops_threshold']:.3f}")
    print(f"  burst_window_count: {summary['burst_window_count']}")
    print(f"  max_window_iops: {summary['max_window_iops']:.3f}")
    print(f"  summary: {result.summary_csv_path}")
    print(f"  windows: {result.window_stats_path}")
    print(f"  bursts: {result.burst_intervals_path}")
    print(f"  hot_lpn: {result.hot_lpn_path}")
    print(f"  manifest: {result.manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
