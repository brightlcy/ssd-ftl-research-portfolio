from __future__ import annotations

import codecs
import csv
import gzip
import io
import json
import math
import sys
import tarfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from itertools import chain
from pathlib import Path
from typing import Dict, Iterator, Optional, Sequence, TextIO, Tuple, Union


PathLike = Union[str, Path]
ALIBABA_IO_COLUMNS = ("device_id", "opcode", "offset", "length", "timestamp")
ALIBABA_DEVICE_SIZE_COLUMNS = ("device_id", "capacity")
STANDARD_TRACE_COLUMNS = ("timestamp", "flow_id", "op", "lba", "size")


class AlibabaTraceFormatError(ValueError):
    """Raised when an Alibaba raw-trace row violates the published schema."""


@dataclass(frozen=True)
class AlibabaRawIO:
    row_number: int
    device_id: int
    opcode: str
    offset_bytes: int
    length_bytes: int
    timestamp_us: int


@dataclass(frozen=True)
class AlibabaConversionStats:
    source_rows: int
    selected_rows: int
    skipped_other_devices: int
    skipped_before_window: int
    skipped_after_window: int
    read_requests: int
    write_requests: int
    read_pages: int
    write_pages: int
    unaligned_requests: int
    device_id: int
    page_size: int
    timestamp_origin_us: int
    min_raw_timestamp_us: int
    max_raw_timestamp_us: int
    min_lpn: int
    max_lpn: int
    flow_id: str
    normalized_timestamps: bool
    skipped_selected_rows: int
    max_write_pages: Optional[int]
    stop_reason: str
    write_page_budget_overshoot: int

    @property
    def duration_s(self) -> float:
        return (self.max_raw_timestamp_us - self.min_raw_timestamp_us) / 1_000_000.0

    def to_dict(self) -> Dict[str, object]:
        data = asdict(self)
        data["duration_s"] = self.duration_s
        return data


@dataclass(frozen=True)
class AlibabaConversionResult:
    output_path: Path
    stats_path: Path
    stats: AlibabaConversionStats


def _source_label(path: Path) -> str:
    return "<stdin>" if str(path) == "-" else str(path.resolve())


def _require_file(path_like: PathLike, *, label: str) -> Path:
    path = Path(path_like).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"{label} does not exist: {path}")
    if not path.is_file():
        raise IsADirectoryError(f"{label} is not a file: {path}")
    return path


@contextmanager
def _open_csv_text(path: Path, *, member_basename: str) -> Iterator[TextIO]:
    """Open stdin, plain CSV, gzip CSV, or one CSV member inside a .tar.gz bundle."""
    if str(path) == "-":
        yield sys.stdin
        return

    lower = path.name.lower()
    if lower.endswith((".tar.gz", ".tgz")):
        archive = tarfile.open(path, mode="r|gz")
        wrapper: Optional[TextIO] = None
        try:
            for member in archive:
                if not member.isfile():
                    continue
                if Path(member.name).name != member_basename:
                    continue
                raw = archive.extractfile(member)
                if raw is None:
                    raise AlibabaTraceFormatError(
                        f"cannot read {member_basename} from archive: {path}"
                    )
                wrapper = codecs.getreader("utf-8-sig")(raw)
                yield wrapper
                return
            raise AlibabaTraceFormatError(
                f"{member_basename} not found in archive: {path}"
            )
        finally:
            if wrapper is not None:
                wrapper.close()
            archive.close()
    elif lower.endswith(".gz"):
        with gzip.open(path, mode="rt", encoding="utf-8-sig", newline="") as handle:
            yield handle
    else:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            yield handle


def _parse_int(
    text: str,
    *,
    column: str,
    row_number: int,
    path: Path,
    minimum: int = 0,
) -> int:
    value = text.strip()
    try:
        parsed = int(value)
    except ValueError as exc:
        raise AlibabaTraceFormatError(
            f"row {row_number}: {column} must be an integer, got {value!r}: {path}"
        ) from exc
    if parsed < minimum:
        relation = ">= 0" if minimum == 0 else f">= {minimum}"
        raise AlibabaTraceFormatError(
            f"row {row_number}: {column} must be {relation}, got {parsed}: {path}"
        )
    return parsed


def _row_index_map(first_row: Sequence[str], path: Path) -> Tuple[Dict[str, int], bool]:
    normalized = [value.strip().lstrip("\ufeff") for value in first_row]
    if all(column in normalized for column in ALIBABA_IO_COLUMNS):
        return {column: normalized.index(column) for column in ALIBABA_IO_COLUMNS}, True

    if len(first_row) != len(ALIBABA_IO_COLUMNS):
        raise AlibabaTraceFormatError(
            "Alibaba trace must either have the published header or exactly five "
            f"columns in published order: {path}; first_row={normalized}"
        )
    return {column: index for index, column in enumerate(ALIBABA_IO_COLUMNS)}, False


def iter_alibaba_io_rows(input_path: PathLike) -> Iterator[AlibabaRawIO]:
    """Yield validated Alibaba block-trace rows without loading the file in memory.

    Published order:
        device_id, opcode, offset(bytes), length(bytes), timestamp(microseconds)

    Both a canonical CSV header and a headerless file in the published order are
    accepted because large trace bundles are often processed with shell tools.
    """
    path = (
        Path("-")
        if str(input_path) == "-"
        else _require_file(input_path, label="Alibaba I/O trace")
    )
    with _open_csv_text(path, member_basename="io_traces.csv") as handle:
        reader = csv.reader(handle)
        try:
            first_row = next(reader)
        except StopIteration as exc:
            raise AlibabaTraceFormatError(f"Alibaba trace is empty: {path}") from exc

        index_map, has_header = _row_index_map(first_row, path)
        data_rows = reader if has_header else chain([first_row], reader)
        first_data_row_number = 2 if has_header else 1

        for row_number, raw in enumerate(data_rows, start=first_data_row_number):
            if not raw or all(not cell.strip() for cell in raw):
                continue
            if len(raw) <= max(index_map.values()):
                raise AlibabaTraceFormatError(
                    f"row {row_number}: expected at least five columns, got {len(raw)}: {path}"
                )

            opcode = raw[index_map["opcode"]].strip().upper()
            if opcode not in {"R", "W"}:
                raise AlibabaTraceFormatError(
                    f"row {row_number}: opcode must be 'R' or 'W', got {opcode!r}: {path}"
                )

            yield AlibabaRawIO(
                row_number=row_number,
                device_id=_parse_int(
                    raw[index_map["device_id"]],
                    column="device_id",
                    row_number=row_number,
                    path=path,
                ),
                opcode=opcode,
                offset_bytes=_parse_int(
                    raw[index_map["offset"]],
                    column="offset",
                    row_number=row_number,
                    path=path,
                ),
                length_bytes=_parse_int(
                    raw[index_map["length"]],
                    column="length",
                    row_number=row_number,
                    path=path,
                    minimum=1,
                ),
                timestamp_us=_parse_int(
                    raw[index_map["timestamp"]],
                    column="timestamp",
                    row_number=row_number,
                    path=path,
                ),
            )


def load_alibaba_device_capacities(input_path: PathLike) -> Dict[int, int]:
    """Load the small official device_size.csv mapping into memory."""
    path = _require_file(input_path, label="Alibaba device-size file")
    capacities: Dict[int, int] = {}
    with _open_csv_text(path, member_basename="device_size.csv") as handle:
        reader = csv.reader(handle)
        try:
            first = next(reader)
        except StopIteration as exc:
            raise AlibabaTraceFormatError(f"device-size file is empty: {path}") from exc

        normalized = [value.strip().lstrip("\ufeff") for value in first]
        if all(column in normalized for column in ALIBABA_DEVICE_SIZE_COLUMNS):
            device_index = normalized.index("device_id")
            capacity_index = normalized.index("capacity")
            rows = reader
            start = 2
        elif len(first) == 2:
            device_index, capacity_index = 0, 1
            rows = chain([first], reader)
            start = 1
        else:
            raise AlibabaTraceFormatError(
                f"invalid device-size schema: {path}; first_row={normalized}"
            )

        for row_number, row in enumerate(rows, start=start):
            if not row or all(not cell.strip() for cell in row):
                continue
            if len(row) <= max(device_index, capacity_index):
                raise AlibabaTraceFormatError(
                    f"row {row_number}: invalid device-size row: {path}"
                )
            device_id = _parse_int(
                row[device_index],
                column="device_id",
                row_number=row_number,
                path=path,
            )
            capacity = _parse_int(
                row[capacity_index],
                column="capacity",
                row_number=row_number,
                path=path,
                minimum=1,
            )
            if device_id in capacities:
                raise AlibabaTraceFormatError(
                    f"row {row_number}: duplicate device_id {device_id}: {path}"
                )
            capacities[device_id] = capacity
    return capacities


def _page_span(offset_bytes: int, length_bytes: int, page_size: int) -> Tuple[int, int]:
    start_lpn = offset_bytes // page_size
    end_lpn_exclusive = math.ceil((offset_bytes + length_bytes) / page_size)
    return start_lpn, end_lpn_exclusive - start_lpn


def _write_stats_json(
    path: Path,
    *,
    source_path: Path,
    result_path: Path,
    device_size_path: Optional[Path],
    stats: AlibabaConversionStats,
) -> None:
    payload = {
        "schema_version": 1,
        "source_format": "alibaba-block-traces-2020",
        "source_path": _source_label(source_path),
        "device_size_path": (
            None if device_size_path is None else str(device_size_path.resolve())
        ),
        "output_standard_trace": str(result_path.resolve()),
        "standard_trace_contract": {
            "timestamp": "seconds, relative to first selected request unless disabled",
            "flow_id": "selected Alibaba virtual disk",
            "op": "R or W",
            "lba": "starting logical page number",
            "size": "number of pages conservatively covering the byte range",
        },
        "conversion_stats": stats.to_dict(),
    }
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def convert_alibaba_device_trace(
    input_path: PathLike,
    output_path: PathLike,
    *,
    device_id: int,
    page_size: int = 4096,
    device_size_path: Optional[PathLike] = None,
    max_rows: Optional[int] = None,
    skip_selected_rows: int = 0,
    max_write_pages: Optional[int] = None,
    start_timestamp_us: Optional[int] = None,
    end_timestamp_us: Optional[int] = None,
    normalize_timestamps: bool = True,
    flow_id: Optional[str] = None,
    stats_path: Optional[PathLike] = None,
) -> AlibabaConversionResult:
    """Stream one Alibaba virtual disk into the simulator's standard CSV.

    The official bundle is extremely large, so this converter never materializes
    the source trace in memory. Selection is intentionally per-device because the
    current FTL mapping namespace is one logical disk. Multi-device composition is
    a later multi-tenant experiment and must not silently alias equal LPNs.
    """
    if device_id < 0:
        raise ValueError(f"device_id must be >= 0: {device_id}")
    if page_size <= 0:
        raise ValueError(f"page_size must be > 0: {page_size}")
    if max_rows is not None and max_rows <= 0:
        raise ValueError(f"max_rows must be > 0 when provided: {max_rows}")
    if skip_selected_rows < 0:
        raise ValueError(f"skip_selected_rows must be >= 0: {skip_selected_rows}")
    if max_write_pages is not None and max_write_pages <= 0:
        raise ValueError(
            f"max_write_pages must be > 0 when provided: {max_write_pages}"
        )
    if start_timestamp_us is not None and start_timestamp_us < 0:
        raise ValueError("start_timestamp_us must be >= 0")
    if end_timestamp_us is not None and end_timestamp_us < 0:
        raise ValueError("end_timestamp_us must be >= 0")
    if (
        start_timestamp_us is not None
        and end_timestamp_us is not None
        and end_timestamp_us <= start_timestamp_us
    ):
        raise ValueError("end_timestamp_us must be greater than start_timestamp_us")

    source = (
        Path("-")
        if str(input_path) == "-"
        else _require_file(input_path, label="Alibaba I/O trace")
    )
    output = Path(output_path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    stats_output = (
        Path(stats_path).expanduser()
        if stats_path is not None
        else output.with_suffix(output.suffix + ".conversion.json")
    )
    stats_output.parent.mkdir(parents=True, exist_ok=True)

    device_size_file: Optional[Path] = None
    capacity_bytes: Optional[int] = None
    if device_size_path is not None:
        device_size_file = _require_file(
            device_size_path, label="Alibaba device-size file"
        )
        capacities = load_alibaba_device_capacities(device_size_file)
        if device_id not in capacities:
            raise KeyError(f"device_id {device_id} not found in {device_size_file}")
        capacity_bytes = capacities[device_id]

    selected_flow_id = flow_id or f"alibaba_device_{device_id}"
    if not selected_flow_id.strip():
        raise ValueError("flow_id must not be empty")

    source_rows = 0
    selected_rows = 0
    skipped_other_devices = 0
    skipped_before_window = 0
    skipped_after_window = 0
    skipped_selected_rows = 0
    eligible_selected_rows = 0
    stop_reason = "source_exhausted"
    read_requests = 0
    write_requests = 0
    read_pages = 0
    write_pages = 0
    unaligned_requests = 0
    timestamp_origin_us: Optional[int] = None
    min_raw_timestamp_us: Optional[int] = None
    max_raw_timestamp_us: Optional[int] = None
    min_lpn: Optional[int] = None
    max_lpn: Optional[int] = None

    try:
        with output.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=STANDARD_TRACE_COLUMNS)
            writer.writeheader()

            for row in iter_alibaba_io_rows(source):
                source_rows += 1
                if row.device_id != device_id:
                    skipped_other_devices += 1
                    continue
                if (
                    start_timestamp_us is not None
                    and row.timestamp_us < start_timestamp_us
                ):
                    skipped_before_window += 1
                    continue
                if end_timestamp_us is not None and row.timestamp_us >= end_timestamp_us:
                    skipped_after_window += 1
                    continue
                if eligible_selected_rows < skip_selected_rows:
                    eligible_selected_rows += 1
                    skipped_selected_rows += 1
                    continue
                eligible_selected_rows += 1
                if max_rows is not None and selected_rows >= max_rows:
                    stop_reason = "max_rows"
                    break

                end_byte = row.offset_bytes + row.length_bytes
                if capacity_bytes is not None and end_byte > capacity_bytes:
                    raise AlibabaTraceFormatError(
                        f"row {row.row_number}: request exceeds device {device_id} "
                        f"capacity: end={end_byte}, capacity={capacity_bytes}: {source}"
                    )

                lba, size_pages = _page_span(
                    row.offset_bytes, row.length_bytes, page_size
                )
                if (
                    row.offset_bytes % page_size != 0
                    or row.length_bytes % page_size != 0
                ):
                    unaligned_requests += 1

                if timestamp_origin_us is None:
                    timestamp_origin_us = row.timestamp_us if normalize_timestamps else 0
                timestamp_s = (row.timestamp_us - timestamp_origin_us) / 1_000_000.0
                if timestamp_s < 0:
                    raise AlibabaTraceFormatError(
                        "selected timestamps moved before the conversion origin; "
                        "input must be timestamp ordered"
                    )

                writer.writerow(
                    {
                        "timestamp": f"{timestamp_s:.6f}",
                        "flow_id": selected_flow_id,
                        "op": row.opcode,
                        "lba": lba,
                        "size": size_pages,
                    }
                )

                selected_rows += 1
                min_raw_timestamp_us = (
                    row.timestamp_us
                    if min_raw_timestamp_us is None
                    else min(min_raw_timestamp_us, row.timestamp_us)
                )
                max_raw_timestamp_us = (
                    row.timestamp_us
                    if max_raw_timestamp_us is None
                    else max(max_raw_timestamp_us, row.timestamp_us)
                )
                min_lpn = lba if min_lpn is None else min(min_lpn, lba)
                request_max_lpn = lba + size_pages - 1
                max_lpn = (
                    request_max_lpn
                    if max_lpn is None
                    else max(max_lpn, request_max_lpn)
                )

                if row.opcode == "R":
                    read_requests += 1
                    read_pages += size_pages
                else:
                    write_requests += 1
                    write_pages += size_pages
                    if max_write_pages is not None and write_pages >= max_write_pages:
                        stop_reason = "max_write_pages"
                        break
    except Exception:
        output.unlink(missing_ok=True)
        raise

    if selected_rows == 0:
        output.unlink(missing_ok=True)
        raise AlibabaTraceFormatError(
            f"no rows selected for device_id={device_id}: {source}"
        )

    assert timestamp_origin_us is not None
    assert min_raw_timestamp_us is not None
    assert max_raw_timestamp_us is not None
    assert min_lpn is not None
    assert max_lpn is not None

    stats = AlibabaConversionStats(
        source_rows=source_rows,
        selected_rows=selected_rows,
        skipped_other_devices=skipped_other_devices,
        skipped_before_window=skipped_before_window,
        skipped_after_window=skipped_after_window,
        read_requests=read_requests,
        write_requests=write_requests,
        read_pages=read_pages,
        write_pages=write_pages,
        unaligned_requests=unaligned_requests,
        device_id=device_id,
        page_size=page_size,
        timestamp_origin_us=timestamp_origin_us,
        min_raw_timestamp_us=min_raw_timestamp_us,
        max_raw_timestamp_us=max_raw_timestamp_us,
        min_lpn=min_lpn,
        max_lpn=max_lpn,
        flow_id=selected_flow_id,
        normalized_timestamps=normalize_timestamps,
        skipped_selected_rows=skipped_selected_rows,
        max_write_pages=max_write_pages,
        stop_reason=stop_reason,
        write_page_budget_overshoot=(
            0 if max_write_pages is None else max(0, write_pages - max_write_pages)
        ),
    )
    _write_stats_json(
        stats_output,
        source_path=source,
        result_path=output,
        device_size_path=device_size_file,
        stats=stats,
    )
    return AlibabaConversionResult(
        output_path=output,
        stats_path=stats_output,
        stats=stats,
    )
