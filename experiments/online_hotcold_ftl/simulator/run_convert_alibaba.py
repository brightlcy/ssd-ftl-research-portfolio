from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

from .alibaba_trace import convert_alibaba_device_trace


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Stream one virtual disk from Alibaba Block Traces 2020 into the "
            "page-based standard trace format used by this FTL simulator."
        )
    )
    parser.add_argument("--input", required=True, help="official io_traces.csv")
    parser.add_argument("--output", required=True, help="standard CSV output path")
    parser.add_argument("--device-id", required=True, type=int)
    parser.add_argument("--device-size-file", default=None)
    parser.add_argument("--page-size", type=int, default=4096)
    parser.add_argument("--max-rows", type=int, default=None)
    parser.add_argument("--skip-selected-rows", type=int, default=0)
    parser.add_argument(
        "--max-write-pages",
        type=int,
        default=None,
        help=(
            "stop after cumulative selected write pages reach this target; "
            "the final request is kept and may cause a small overshoot"
        ),
    )
    parser.add_argument("--start-timestamp-us", type=int, default=None)
    parser.add_argument("--end-timestamp-us", type=int, default=None)
    parser.add_argument("--flow-id", default=None)
    parser.add_argument("--stats-output", default=None)
    parser.add_argument(
        "--keep-absolute-time",
        action="store_true",
        help="write timestamp_us / 1e6 instead of rebasing first selected row to 0",
    )
    return parser


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        result = convert_alibaba_device_trace(
            args.input,
            args.output,
            device_id=args.device_id,
            page_size=args.page_size,
            device_size_path=args.device_size_file,
            max_rows=args.max_rows,
            skip_selected_rows=args.skip_selected_rows,
            max_write_pages=args.max_write_pages,
            start_timestamp_us=args.start_timestamp_us,
            end_timestamp_us=args.end_timestamp_us,
            normalize_timestamps=not args.keep_absolute_time,
            flow_id=args.flow_id,
            stats_path=args.stats_output,
        )
    except Exception as exc:
        print(f"Alibaba trace conversion failed: {exc}", file=sys.stderr)
        return 1

    stats = result.stats
    print("Alibaba trace conversion completed")
    print(f"  device_id: {stats.device_id}")
    print(f"  selected_rows: {stats.selected_rows}")
    print(f"  read_requests: {stats.read_requests}")
    print(f"  write_requests: {stats.write_requests}")
    print(f"  read_pages: {stats.read_pages}")
    print(f"  write_pages: {stats.write_pages}")
    print(f"  unaligned_requests: {stats.unaligned_requests}")
    print(f"  duration_s: {stats.duration_s:.6f}")
    print(f"  stop_reason: {stats.stop_reason}")
    if stats.max_write_pages is not None:
        print(f"  max_write_pages: {stats.max_write_pages}")
        print(f"  write_page_budget_overshoot: {stats.write_page_budget_overshoot}")
    print(f"  output: {result.output_path}")
    print(f"  conversion_stats: {result.stats_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
