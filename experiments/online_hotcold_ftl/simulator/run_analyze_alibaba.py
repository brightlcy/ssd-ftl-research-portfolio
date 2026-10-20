from __future__ import annotations

import argparse
import sys
from typing import Mapping, Optional, Sequence

from .alibaba_workload import analyze_alibaba_workloads


def _format_seconds(value: Optional[float]) -> str:
    if value is None:
        return "unknown"
    seconds = max(0, int(value))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _print_progress(payload: Mapping[str, object]) -> None:
    rows = int(payload["source_rows"])
    devices = int(payload["analyzed_devices"])
    elapsed = float(payload["wall_elapsed_s"])
    rate = float(payload["rows_per_second"])
    eta = payload.get("eta_s")
    trace_duration = float(payload["trace_duration_s"])
    checkpoint = " checkpoint=saved" if payload.get("checkpoint_written") else ""
    print(
        "[Alibaba progress] "
        f"rows={rows:,} devices={devices} "
        f"rate={rate:,.0f} rows/s elapsed={_format_seconds(elapsed)} "
        f"trace_time={_format_seconds(trace_duration)} "
        f"eta={_format_seconds(None if eta is None else float(eta))}"
        f"{checkpoint}",
        file=sys.stderr,
        flush=True,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Stream the Alibaba trace once, build per-device workload summaries, "
            "show progress, and save atomic checkpoints."
        )
    )
    parser.add_argument(
        "--input",
        required=True,
        help="io_traces.csv, .csv.gz, official .tar.gz, or - for decompressed stdin",
    )
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--device-size-file", default=None)
    parser.add_argument("--page-size", type=int, default=4096)
    parser.add_argument("--short-iat-us", type=int, default=1000)
    parser.add_argument("--max-source-rows", type=int, default=None)
    parser.add_argument(
        "--max-duration-seconds",
        type=float,
        default=None,
        help="stop after this much globally timestamp-ordered source time",
    )
    parser.add_argument("--minimum-candidate-requests", type=int, default=1000)
    parser.add_argument(
        "--progress-every-rows",
        type=int,
        default=1_000_000,
        help="print progress every N source rows; default 1,000,000",
    )
    parser.add_argument(
        "--checkpoint-every-rows",
        type=int,
        default=5_000_000,
        help="atomically save partial summaries every N source rows; default 5,000,000",
    )
    return parser


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        result = analyze_alibaba_workloads(
            args.input,
            args.output_dir,
            device_size_path=args.device_size_file,
            page_size=args.page_size,
            short_iat_us=args.short_iat_us,
            max_source_rows=args.max_source_rows,
            max_duration_seconds=args.max_duration_seconds,
            minimum_candidate_requests=args.minimum_candidate_requests,
            progress_every_rows=args.progress_every_rows,
            checkpoint_every_rows=args.checkpoint_every_rows,
            progress_callback=_print_progress,
        )
    except Exception as exc:
        print(f"Alibaba workload analysis failed: {exc}", file=sys.stderr)
        return 1

    if result.stop_reason == "interrupted":
        print("Alibaba workload analysis interrupted; partial results were saved")
    else:
        print("Alibaba workload analysis completed")
    print(f"  source_rows: {result.source_rows}")
    print(f"  analyzed_devices: {result.analyzed_devices}")
    print(f"  analyzed_duration_s: {result.analyzed_duration_s:.3f}")
    print(f"  stop_reason: {result.stop_reason}")
    for item in result.candidates:
        print(
            f"  candidate[{item.candidate_role}]: device={item.device_id}, "
            f"write_ratio={item.write_ratio:.4f}, requests={item.request_count}"
        )
    print(f"  device_summary: {result.device_summary_path}")
    print(f"  candidates: {result.candidate_path}")
    print(f"  dataset_summary: {result.dataset_summary_path}")
    return 130 if result.stop_reason == "interrupted" else 0


if __name__ == "__main__":
    raise SystemExit(main())
