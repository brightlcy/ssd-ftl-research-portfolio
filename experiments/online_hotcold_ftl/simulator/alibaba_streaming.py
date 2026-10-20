from __future__ import annotations

import csv
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional, Sequence, Tuple, Union

from .alibaba_trace import STANDARD_TRACE_COLUMNS, iter_alibaba_io_rows


PathLike = Union[str, Path]


@dataclass
class _MutableDeviceStats:
    device_id: int
    selected_rows: int = 0
    read_requests: int = 0
    write_requests: int = 0
    read_pages: int = 0
    write_pages: int = 0
    unaligned_requests: int = 0
    first_timestamp_us: Optional[int] = None
    last_timestamp_us: Optional[int] = None
    min_lpn: Optional[int] = None
    max_lpn: Optional[int] = None
    stop_reason: str = "source_exhausted"


@dataclass(frozen=True)
class AlibabaExtractedDevice:
    device_id: int
    output_path: Path
    selected_rows: int
    read_requests: int
    write_requests: int
    read_pages: int
    write_pages: int
    unaligned_requests: int
    first_timestamp_us: Optional[int]
    last_timestamp_us: Optional[int]
    duration_s: float
    min_lpn: Optional[int]
    max_lpn: Optional[int]
    stop_reason: str

    def to_dict(self) -> Dict[str, object]:
        data = asdict(self)
        data["output_path"] = str(self.output_path.resolve())
        return data


@dataclass(frozen=True)
class AlibabaMultiExtractResult:
    output_dir: Path
    manifest_path: Path
    source_rows: int
    analyzed_duration_s: float
    stop_reason: str
    devices: Tuple[AlibabaExtractedDevice, ...]


def _page_span(offset_bytes: int, length_bytes: int, page_size: int) -> Tuple[int, int]:
    start_lpn = offset_bytes // page_size
    end_lpn_exclusive = math.ceil((offset_bytes + length_bytes) / page_size)
    return start_lpn, end_lpn_exclusive - start_lpn


def load_candidate_device_ids(path_like: PathLike) -> Tuple[int, ...]:
    path = Path(path_like).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"candidate CSV does not exist: {path}")
    ids = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or "device_id" not in reader.fieldnames:
            raise ValueError(f"candidate CSV must contain device_id: {path}")
        for row_number, row in enumerate(reader, start=2):
            try:
                device_id = int((row.get("device_id") or "").strip())
            except ValueError as exc:
                raise ValueError(
                    f"row {row_number}: invalid device_id in candidate CSV: {path}"
                ) from exc
            ids.append(device_id)
    if not ids:
        raise ValueError(f"candidate CSV has no devices: {path}")
    return tuple(dict.fromkeys(ids))


def extract_alibaba_devices(
    input_path: PathLike,
    output_dir: PathLike,
    *,
    device_ids: Sequence[int],
    page_size: int = 4096,
    max_duration_seconds: Optional[float] = None,
    max_rows_per_device: Optional[int] = None,
    max_write_pages_per_device: Optional[int] = None,
) -> AlibabaMultiExtractResult:
    """Extract several Alibaba devices in one decompressed streaming pass.

    Input may be ``-`` so a shell pipeline can stream ``aws s3 cp ... - | zstd -dc``
    directly into the converter without storing the 140 GiB compressed corpus.
    """
    unique_ids = tuple(dict.fromkeys(int(value) for value in device_ids))
    if not unique_ids:
        raise ValueError("at least one device_id is required")
    if any(value < 0 for value in unique_ids):
        raise ValueError("device_ids must be >= 0")
    if page_size <= 0:
        raise ValueError("page_size must be > 0")
    if max_duration_seconds is not None and max_duration_seconds <= 0:
        raise ValueError("max_duration_seconds must be > 0 when provided")
    if max_rows_per_device is not None and max_rows_per_device <= 0:
        raise ValueError("max_rows_per_device must be > 0 when provided")
    if max_write_pages_per_device is not None and max_write_pages_per_device <= 0:
        raise ValueError("max_write_pages_per_device must be > 0 when provided")

    out = Path(output_dir).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    selected = set(unique_ids)
    mutable = {device_id: _MutableDeviceStats(device_id) for device_id in unique_ids}
    paths = {
        device_id: out / f"device_{device_id}.standard.csv"
        for device_id in unique_ids
    }
    handles = {}
    writers = {}
    for device_id in unique_ids:
        handle = paths[device_id].open("w", encoding="utf-8", newline="")
        writer = csv.DictWriter(handle, fieldnames=STANDARD_TRACE_COLUMNS)
        writer.writeheader()
        handles[device_id] = handle
        writers[device_id] = writer

    source_rows = 0
    source_first_ts: Optional[int] = None
    source_last_ts: Optional[int] = None
    previous_source_ts: Optional[int] = None
    globally_done = set()
    stop_reason = "source_exhausted"

    try:
        for row in iter_alibaba_io_rows(input_path):
            if (
                max_duration_seconds is not None
                and previous_source_ts is not None
                and row.timestamp_us < previous_source_ts
            ):
                raise ValueError(
                    "Alibaba source timestamps must be globally non-decreasing for "
                    "duration-bounded extraction"
                )
            previous_source_ts = row.timestamp_us
            if source_first_ts is None:
                source_first_ts = row.timestamp_us
            if (
                max_duration_seconds is not None
                and row.timestamp_us - source_first_ts
                >= int(max_duration_seconds * 1_000_000)
            ):
                stop_reason = "max_duration_seconds"
                break

            source_rows += 1
            source_last_ts = row.timestamp_us
            if row.device_id not in selected or row.device_id in globally_done:
                continue

            stats = mutable[row.device_id]
            lba, size_pages = _page_span(row.offset_bytes, row.length_bytes, page_size)
            if row.offset_bytes % page_size or row.length_bytes % page_size:
                stats.unaligned_requests += 1
            if stats.first_timestamp_us is None:
                stats.first_timestamp_us = row.timestamp_us
            stats.last_timestamp_us = row.timestamp_us
            timestamp_s = (row.timestamp_us - stats.first_timestamp_us) / 1_000_000.0
            writers[row.device_id].writerow(
                {
                    "timestamp": f"{timestamp_s:.6f}",
                    "flow_id": f"alibaba_device_{row.device_id}",
                    "op": row.opcode,
                    "lba": lba,
                    "size": size_pages,
                }
            )
            stats.selected_rows += 1
            stats.min_lpn = lba if stats.min_lpn is None else min(stats.min_lpn, lba)
            request_max_lpn = lba + size_pages - 1
            stats.max_lpn = (
                request_max_lpn
                if stats.max_lpn is None
                else max(stats.max_lpn, request_max_lpn)
            )
            if row.opcode == "R":
                stats.read_requests += 1
                stats.read_pages += size_pages
            else:
                stats.write_requests += 1
                stats.write_pages += size_pages

            if max_rows_per_device is not None and stats.selected_rows >= max_rows_per_device:
                stats.stop_reason = "max_rows_per_device"
                globally_done.add(row.device_id)
            elif (
                max_write_pages_per_device is not None
                and stats.write_pages >= max_write_pages_per_device
            ):
                stats.stop_reason = "max_write_pages_per_device"
                globally_done.add(row.device_id)

            if globally_done == selected:
                stop_reason = "all_devices_reached_budget"
                break
    finally:
        for handle in handles.values():
            handle.close()

    devices = []
    for device_id in unique_ids:
        stats = mutable[device_id]
        if stats.selected_rows == 0:
            paths[device_id].unlink(missing_ok=True)
        duration_s = (
            0.0
            if stats.first_timestamp_us is None or stats.last_timestamp_us is None
            else (stats.last_timestamp_us - stats.first_timestamp_us) / 1_000_000.0
        )
        devices.append(
            AlibabaExtractedDevice(
                device_id=device_id,
                output_path=paths[device_id],
                selected_rows=stats.selected_rows,
                read_requests=stats.read_requests,
                write_requests=stats.write_requests,
                read_pages=stats.read_pages,
                write_pages=stats.write_pages,
                unaligned_requests=stats.unaligned_requests,
                first_timestamp_us=stats.first_timestamp_us,
                last_timestamp_us=stats.last_timestamp_us,
                duration_s=duration_s,
                min_lpn=stats.min_lpn,
                max_lpn=stats.max_lpn,
                stop_reason=stats.stop_reason,
            )
        )

    analyzed_duration_s = (
        0.0
        if source_first_ts is None or source_last_ts is None
        else (source_last_ts - source_first_ts) / 1_000_000.0
    )
    manifest_path = out / "alibaba_multi_extract_manifest.json"
    payload = {
        "schema_version": 1,
        "source_path": "<stdin>" if str(input_path) == "-" else str(Path(input_path).expanduser().resolve()),
        "source_rows": source_rows,
        "analyzed_duration_s": analyzed_duration_s,
        "stop_reason": stop_reason,
        "page_size": page_size,
        "max_duration_seconds": max_duration_seconds,
        "max_rows_per_device": max_rows_per_device,
        "max_write_pages_per_device": max_write_pages_per_device,
        "devices": [item.to_dict() for item in devices],
        "methodological_note": (
            "Multiple devices are extracted in one source pass, but each output keeps an "
            "independent logical-page namespace and per-device timestamp origin."
        ),
    }
    with manifest_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")

    return AlibabaMultiExtractResult(
        output_dir=out,
        manifest_path=manifest_path,
        source_rows=source_rows,
        analyzed_duration_s=analyzed_duration_s,
        stop_reason=stop_reason,
        devices=tuple(devices),
    )
