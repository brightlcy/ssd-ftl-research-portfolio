from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

from .alibaba_ebs_metadata import write_pilot_plan


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Select download-friendly Alibaba Cloud EBS 2026 pilot traces from "
            "metadata. Selection by file size is for acquisition only, not final "
            "workload representativeness."
        )
    )
    parser.add_argument("--metadata", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--workloads", default="mysql,mq,k8s")
    parser.add_argument("--per-workload", type=int, default=2)
    parser.add_argument("--min-size-mb", type=float, default=64.0)
    parser.add_argument("--max-size-mb", type=float, default=160.0)
    parser.add_argument("--target-size-mb", type=float, default=100.0)
    return parser


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    workloads = [item.strip() for item in args.workloads.split(",") if item.strip()]
    try:
        result = write_pilot_plan(
            args.metadata,
            args.output_dir,
            workloads=workloads,
            per_workload=args.per_workload,
            min_size_mb=args.min_size_mb,
            max_size_mb=args.max_size_mb,
            target_size_mb=args.target_size_mb,
        )
    except Exception as exc:
        print(f"Alibaba EBS pilot selection failed: {exc}", file=sys.stderr)
        return 1

    print("Alibaba EBS pilot selection completed")
    print(f"  metadata_rows: {result.row_count}")
    print(f"  candidate_count: {len(result.candidates)}")
    for item in result.candidates:
        print(
            f"  candidate: {item.expected_relative_path} "
            f"size={item.trace_size_mb:.0f}MB"
        )
    print(f"  candidates: {result.candidate_path}")
    print(f"  workload_summary: {result.summary_path}")
    print(f"  manifest: {result.manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
