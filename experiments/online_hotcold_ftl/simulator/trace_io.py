from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Mapping, Optional, Sequence, Union

from .config import SSDConfig


REQUIRED_TRACE_COLUMNS = ("timestamp", "flow_id", "op", "lba", "size")
CAPACITY_BASIS_WRITE_WORKING_SET = "write-working-set"
CAPACITY_BASIS_ADDRESS_SPAN = "address-span"
CAPACITY_BASIS_CHOICES = (
    CAPACITY_BASIS_WRITE_WORKING_SET,
    CAPACITY_BASIS_ADDRESS_SPAN,
)
PathLike = Union[str, Path]
TraceRow = Dict[str, Any]


class TraceFormatError(ValueError):
    """Raised when a standard trace row violates the input contract."""


@dataclass(frozen=True)
class TraceStats:
    """Summary required to size the simulated SSD and verify trace conversion.

    The standard trace contract is page based:
      * lba: starting logical page number (LPN)
      * size: number of logical pages in the request
    """

    total_requests: int
    read_requests: int
    write_requests: int
    read_pages: int
    write_pages: int
    logical_pages: int
    write_working_set_pages: int
    touched_working_set_pages: int
    min_lpn: int
    max_lpn: int
    min_timestamp: float
    max_timestamp: float
    flow_ids: tuple[str, ...]

    @property
    def duration(self) -> float:
        return self.max_timestamp - self.min_timestamp

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["duration"] = self.duration
        # JSON has no tuple type. A list is clearer in the saved artifact.
        data["flow_ids"] = list(self.flow_ids)
        return data


def _require_trace_file(input_path: PathLike) -> Path:
    path = Path(input_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"trace file does not exist: {path}")
    if not path.is_file():
        raise IsADirectoryError(f"trace path is not a file: {path}")
    return path


def _validate_header(fieldnames: Optional[list[str]], path: Path) -> None:
    if fieldnames is None:
        raise TraceFormatError(f"trace has no CSV header: {path}")

    normalized = [name.strip().lstrip("\ufeff") for name in fieldnames]
    missing = [name for name in REQUIRED_TRACE_COLUMNS if name not in normalized]
    if missing:
        raise TraceFormatError(
            f"trace header is missing required columns {missing}: {path}; "
            f"found={normalized}"
        )


def _parse_nonnegative_float(
    value: Optional[str], *, column: str, row_number: int, path: Path
) -> float:
    text = "" if value is None else value.strip()
    try:
        parsed = float(text)
    except ValueError as exc:
        raise TraceFormatError(
            f"row {row_number}: {column} must be a number, got {text!r}: {path}"
        ) from exc
    if parsed < 0:
        raise TraceFormatError(
            f"row {row_number}: {column} must be >= 0, got {parsed}: {path}"
        )
    return parsed


def _parse_int(
    value: Optional[str],
    *,
    column: str,
    row_number: int,
    path: Path,
    minimum: int,
) -> int:
    text = "" if value is None else value.strip()
    try:
        parsed = int(text)
    except ValueError as exc:
        raise TraceFormatError(
            f"row {row_number}: {column} must be an integer, got {text!r}: {path}"
        ) from exc
    if parsed < minimum:
        comparator = ">= 0" if minimum == 0 else "> 0"
        raise TraceFormatError(
            f"row {row_number}: {column} must be {comparator}, got {parsed}: {path}"
        )
    return parsed


def iter_standard_trace(input_path: PathLike) -> Iterator[TraceRow]:
    """Yield validated rows from a page-based standard trace CSV.

    Required header:
        timestamp,flow_id,op,lba,size

    Returned row types:
        timestamp -> float
        flow_id   -> str
        op        -> "R" or "W"
        lba       -> int (starting LPN, >= 0)
        size      -> int (page count, > 0)

    Empty physical lines are ignored. Invalid rows fail fast with their CSV row
    number so a trace converter bug cannot silently contaminate an experiment.
    """

    path = _require_trace_file(input_path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        _validate_header(reader.fieldnames, path)

        # DictReader preserves the original header keys. Strip them once so BOM
        # or accidental surrounding spaces do not break access by canonical name.
        key_map = {
            original: original.strip().lstrip("\ufeff")
            for original in (reader.fieldnames or [])
        }

        for row_number, raw_row in enumerate(reader, start=2):
            if raw_row is None or all(
                value is None or not value.strip() for value in raw_row.values()
            ):
                continue

            row = {key_map[key]: value for key, value in raw_row.items() if key is not None}

            flow_id = (row.get("flow_id") or "").strip()
            if not flow_id:
                raise TraceFormatError(
                    f"row {row_number}: flow_id must not be empty: {path}"
                )

            op = (row.get("op") or "").strip().upper()
            if op not in {"R", "W"}:
                raise TraceFormatError(
                    f"row {row_number}: op must be 'R' or 'W', got {op!r}: {path}"
                )

            yield {
                "timestamp": _parse_nonnegative_float(
                    row.get("timestamp"),
                    column="timestamp",
                    row_number=row_number,
                    path=path,
                ),
                "flow_id": flow_id,
                "op": op,
                "lba": _parse_int(
                    row.get("lba"),
                    column="lba",
                    row_number=row_number,
                    path=path,
                    minimum=0,
                ),
                "size": _parse_int(
                    row.get("size"),
                    column="size",
                    row_number=row_number,
                    path=path,
                    minimum=1,
                ),
            }


def _count_interval_union_pages(intervals: Sequence[tuple[int, int]]) -> int:
    """Return the number of distinct integer pages covered by half-open intervals."""
    if not intervals:
        return 0

    ordered = sorted(intervals)
    current_start, current_end = ordered[0]
    total = 0

    for start, end in ordered[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
            continue
        total += current_end - current_start
        current_start, current_end = start, end

    return total + (current_end - current_start)


def scan_trace_stats(input_path: PathLike) -> TraceStats:
    """Scan a standard trace once and return sizing/verification statistics.

    `logical_pages` remains the raw address span (`max(lba + size)`) for
    trace diagnostics and backward compatibility. Sparse enterprise traces can
    have a very large address span while touching only a small working set, so
    two additional exact interval-union counts are also recorded:

      * write_working_set_pages: distinct LPNs referenced by writes
      * touched_working_set_pages: distinct LPNs referenced by reads or writes

    Baseline v1 sizes the SSD from the write working set by default because only
    writes allocate NAND pages in the current functional model.
    """

    total_requests = 0
    read_requests = 0
    write_requests = 0
    read_pages = 0
    write_pages = 0
    logical_pages = 0
    min_lpn: Optional[int] = None
    max_lpn: Optional[int] = None
    min_timestamp: Optional[float] = None
    max_timestamp: Optional[float] = None
    flow_ids: set[str] = set()
    write_intervals: list[tuple[int, int]] = []
    touched_intervals: list[tuple[int, int]] = []

    for row in iter_standard_trace(input_path):
        total_requests += 1
        timestamp = row["timestamp"]
        lba = row["lba"]
        size = row["size"]
        end_lpn_exclusive = lba + size
        last_lpn = end_lpn_exclusive - 1

        min_timestamp = timestamp if min_timestamp is None else min(min_timestamp, timestamp)
        max_timestamp = timestamp if max_timestamp is None else max(max_timestamp, timestamp)
        min_lpn = lba if min_lpn is None else min(min_lpn, lba)
        max_lpn = last_lpn if max_lpn is None else max(max_lpn, last_lpn)
        logical_pages = max(logical_pages, end_lpn_exclusive)
        flow_ids.add(row["flow_id"])
        touched_intervals.append((lba, end_lpn_exclusive))

        if row["op"] == "W":
            write_intervals.append((lba, end_lpn_exclusive))
            write_requests += 1
            write_pages += size
        else:
            read_requests += 1
            read_pages += size

    if total_requests == 0:
        path = Path(input_path).expanduser()
        raise TraceFormatError(f"trace contains no data rows: {path}")

    # Values cannot be None after at least one validated row.
    assert min_lpn is not None
    assert max_lpn is not None
    assert min_timestamp is not None
    assert max_timestamp is not None

    return TraceStats(
        total_requests=total_requests,
        read_requests=read_requests,
        write_requests=write_requests,
        read_pages=read_pages,
        write_pages=write_pages,
        logical_pages=logical_pages,
        write_working_set_pages=_count_interval_union_pages(write_intervals),
        touched_working_set_pages=_count_interval_union_pages(touched_intervals),
        min_lpn=min_lpn,
        max_lpn=max_lpn,
        min_timestamp=min_timestamp,
        max_timestamp=max_timestamp,
        flow_ids=tuple(sorted(flow_ids)),
    )


def resolve_ssd_config(
    input_path: PathLike,
    *,
    page_size: int = 4096,
    chips: int = 4,
    pages_per_block: int = 64,
    overprovision_ratio: float = 0.20,
    gc_threshold_ratio: float = 0.05,
    reserved_gc_blocks: int = 2,
    capacity_basis: str = CAPACITY_BASIS_WRITE_WORKING_SET,
) -> tuple[TraceStats, SSDConfig]:
    """Scan a trace and resolve one deterministic SSD geometry from it.

    `write-working-set` is the correct default for Baseline v1: original sparse
    LPN values remain unchanged as mapping keys, while physical capacity is based
    on the number of distinct pages that can actually become mapped.

    `address-span` preserves the previous max-LPN-based behavior for explicit
    comparison or when a known full-volume address span is desired.
    """
    if capacity_basis not in CAPACITY_BASIS_CHOICES:
        raise ValueError(
            f"capacity_basis must be one of {CAPACITY_BASIS_CHOICES}: "
            f"{capacity_basis!r}"
        )

    stats = scan_trace_stats(input_path)
    if capacity_basis == CAPACITY_BASIS_WRITE_WORKING_SET:
        capacity_pages = stats.write_working_set_pages
        if capacity_pages == 0:
            raise ValueError(
                "write-working-set sizing requires at least one write page"
            )
    else:
        capacity_pages = stats.logical_pages

    config = SSDConfig.from_logical_pages(
        logical_pages=capacity_pages,
        page_size=page_size,
        chips=chips,
        pages_per_block=pages_per_block,
        overprovision_ratio=overprovision_ratio,
        gc_threshold_ratio=gc_threshold_ratio,
        reserved_gc_blocks=reserved_gc_blocks,
        capacity_basis=capacity_basis,
    )
    return stats, config


def save_resolved_config(
    output_dir: PathLike,
    config: SSDConfig,
    *,
    trace_stats: Optional[TraceStats] = None,
    source_trace: Optional[PathLike] = None,
    filename: str = "resolved_config.json",
) -> Path:
    """Save the exact trace/config contract used by an experiment.

    Baseline and proposed policies should both load or record this same resolved
    configuration so SSD geometry does not become an uncontrolled variable.
    """

    if not filename.strip():
        raise ValueError("filename must not be empty")

    directory = Path(output_dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    output_path = directory / filename

    payload: Dict[str, Any] = {"ssd_config": config.to_dict()}
    if trace_stats is not None:
        payload["trace_stats"] = trace_stats.to_dict()
    if source_trace is not None:
        payload["source_trace"] = str(Path(source_trace).expanduser().resolve())

    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")

    return output_path


GC_LOG_FIELDS = (
    "gc_id",
    "victim_block",
    "valid_pages_before",
    "invalid_pages_before",
    "free_pages_before",
    "migrated_pages",
    "free_pages_after",
    "victim_erase_count",
    "net_reclaimed_pages",
)


def _prepare_output_path(
    output_dir: PathLike,
    filename: str,
) -> Path:
    if not filename or not filename.strip():
        raise ValueError("filename must not be empty")

    directory = Path(output_dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    return directory / filename


def save_summary(
    output_dir: PathLike,
    summary_dict: Mapping[str, Any],
    *,
    filename: str = "baseline_summary.csv",
) -> Path:
    """Save one baseline experiment summary as a single-row CSV.

    Column order follows the mapping insertion order. BaselineFTL.get_summary()
    returns a stable order, so downstream scripts receive deterministic columns.
    """
    if not summary_dict:
        raise ValueError("summary_dict must not be empty")

    output_path = _prepare_output_path(output_dir, filename)
    fieldnames = list(summary_dict.keys())

    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow(dict(summary_dict))

    return output_path


def save_gc_log(
    output_dir: PathLike,
    gc_log_list: Iterable[Mapping[str, Any]],
    *,
    filename: str = "gc_log.csv",
) -> Path:
    """Save one row per completed GC cycle.

    A header-only file is intentionally created when no GC occurred. This makes
    automation deterministic: every successful experiment emits the same set of
    artifact files regardless of whether the workload triggered collection.
    """
    output_path = _prepare_output_path(output_dir, filename)
    rows = [dict(row) for row in gc_log_list]

    for index, row in enumerate(rows, start=1):
        missing = [field for field in GC_LOG_FIELDS if field not in row]
        extra = [field for field in row if field not in GC_LOG_FIELDS]
        if missing or extra:
            raise ValueError(
                "invalid GC log row "
                f"{index}: missing={missing}, extra={extra}"
            )

    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(GC_LOG_FIELDS))
        writer.writeheader()
        writer.writerows(rows)

    return output_path
