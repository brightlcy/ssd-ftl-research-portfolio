from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence, Tuple

from .alibaba_streaming import extract_alibaba_devices, load_candidate_device_ids


def _parse_device_ids(text: str) -> Tuple[int, ...]:
    values = []
    for token in text.split(","):
        token = token.strip()
        if token:
            values.append(int(token))
    if not values:
        raise argparse.ArgumentTypeError("provide at least one comma-separated device id")
    return tuple(values)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Extract several Alibaba virtual disks in one decompressed source pass. "
            "Use --input - with aws s3 cp ... - | zstd -dc."
        )
    )
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--device-ids", type=_parse_device_ids)
    group.add_argument("--candidates-csv")
    parser.add_argument("--page-size", type=int, default=4096)
    parser.add_argument("--max-duration-seconds", type=float, default=None)
    parser.add_argument("--max-rows-per-device", type=int, default=None)
    parser.add_argument("--max-write-pages-per-device", type=int, default=None)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        device_ids = (
            args.device_ids
            if args.device_ids is not None
            else load_candidate_device_ids(args.candidates_csv)
        )
        result = extract_alibaba_devices(
            args.input,
            args.output_dir,
            device_ids=device_ids,
            page_size=args.page_size,
            max_duration_seconds=args.max_duration_seconds,
            max_rows_per_device=args.max_rows_per_device,
            max_write_pages_per_device=args.max_write_pages_per_device,
        )
    except Exception as exc:
        print(f"Alibaba multi-device extraction failed: {exc}", file=sys.stderr)
        return 1

    print("Alibaba multi-device extraction completed")
    print(f"  source_rows: {result.source_rows}")
    print(f"  analyzed_duration_s: {result.analyzed_duration_s:.3f}")
    print(f"  stop_reason: {result.stop_reason}")
    for item in result.devices:
        print(
            f"  device={item.device_id} rows={item.selected_rows} "
            f"writes={item.write_requests} write_pages={item.write_pages} "
            f"output={item.output_path}"
        )
    print(f"  manifest: {result.manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
