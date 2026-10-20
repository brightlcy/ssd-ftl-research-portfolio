from __future__ import annotations

import csv
import gzip
import json
import math
import re
import zipfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterator, Optional, Sequence, TextIO, Tuple, Union

from .alibaba_trace import AlibabaTraceFormatError, STANDARD_TRACE_COLUMNS


PathLike = Union[str, Path]
EBS_TRACE_COLUMNS = ("op", "dev_offset", "length", "start_time", "end_time")
_FILENAME_PATTERN = re.compile(r"cluster(?P<cluster>\d+)-dev(?P<volume>\d+)\.csv$", re.IGNORECASE)


@dataclass(frozen=True)
class AlibabaEBSRawIO:
    row_number: int
    opcode: str
    offset_bytes: int
    length_bytes: int
    start_time_us: int
    end_time_us: int

    @property
    def observed_latency_us(self) -> int:
        return self.end_time_us - self.start_time_us


@dataclass(frozen=True)
class AlibabaEBSTraceIdentity:
    workload_type: str
    cluster_id: int
    volume_id: int

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class AlibabaEBSConversionStats:
    source_rows: int
    selected_rows: int
    skipped_rows: int
    read_requests: int
    write_requests: int
    read_pages: int
    write_pages: int
    unaligned_requests: int
    page_size: int
    min_raw_timestamp_us: int
    max_raw_timestamp_us: int
    min_lpn: int
    max_lpn: int
    flow_id: str
    workload_type: str
    cluster_id: int
    volume_id: int
    normalized_timestamps: bool
    stop_reason: str
    observed_latency_avg_us: float
    observed_latency_min_us: int
    observed_latency_max_us: int

    @property
    def duration_s(self) -> float:
        return (self.max_raw_timestamp_us - self.min_raw_timestamp_us) / 1_000_000.0

    def to_dict(self) -> Dict[str, object]:
        data = asdict(self)
        data["duration_s"] = self.duration_s
        return data


@dataclass(frozen=True)
class AlibabaEBSConversionResult:
    output_path: Path
    stats_path: Path
    stats: AlibabaEBSConversionStats


def _require_file(path_like: PathLike, *, label: str) -> Path:
    path = Path(path_like).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"{label} does not exist: {path}")
    if not path.is_file():
        raise IsADirectoryError(f"{label} is not a file: {path}")
    return path


def _parse_int(
    value: str,
    *,
    column: str,
    row_number: int,
    path: Path,
    minimum: int = 0,
) -> int:
    text = value.strip()
    try:
        parsed = int(text)
    except ValueError as exc:
        raise AlibabaTraceFormatError(
            f"row {row_number}: {column} must be an integer, got {text!r}: {path}"
        ) from exc
    if parsed < minimum:
        relation = ">= 0" if minimum == 0 else f">= {minimum}"
        raise AlibabaTraceFormatError(
            f"row {row_number}: {column} must be {relation}, got {parsed}: {path}"
        )
    return parsed


def infer_ebs_trace_identity(
    input_path: PathLike,
    *,
    workload_type: Optional[str] = None,
    cluster_id: Optional[int] = None,
    volume_id: Optional[int] = None,
    member_name: Optional[str] = None,
) -> AlibabaEBSTraceIdentity:
    path = Path(input_path)
    identity_source = Path(member_name) if member_name else path
    match = _FILENAME_PATTERN.search(identity_source.name)

    inferred_cluster = int(match.group("cluster")) if match else None
    inferred_volume = int(match.group("volume")) if match else None
    inferred_workload = identity_source.parent.name.strip().lower()
    if not inferred_workload or inferred_workload == ".":
        inferred_workload = path.parent.name.strip().lower()

    resolved_workload = (workload_type or inferred_workload).strip().lower()
    resolved_cluster = cluster_id if cluster_id is not None else inferred_cluster
    resolved_volume = volume_id if volume_id is not None else inferred_volume

    if not resolved_workload:
        raise ValueError("workload_type could not be inferred; pass --workload-type")
    if resolved_cluster is None:
        raise ValueError("cluster_id could not be inferred; pass --cluster-id")
    if resolved_volume is None:
        raise ValueError("volume_id could not be inferred; pass --volume-id")
    if resolved_cluster < 0 or resolved_volume < 0:
        raise ValueError("cluster_id and volume_id must be >= 0")

    return AlibabaEBSTraceIdentity(
        workload_type=resolved_workload,
        cluster_id=resolved_cluster,
        volume_id=resolved_volume,
    )


@contextmanager
def _open_ebs_text(path: Path, *, member_name: Optional[str]) -> Iterator[TextIO]:
    lower = path.name.lower()
    if lower.endswith(".zip"):
        archive = zipfile.ZipFile(path, mode="r")
        wrapper: Optional[TextIO] = None
        try:
            csv_members = [name for name in archive.namelist() if name.lower().endswith(".csv")]
            selected = member_name
            if selected is None:
                if len(csv_members) != 1:
                    preview = ", ".join(csv_members[:5])
                    raise AlibabaTraceFormatError(
                        "zip input contains multiple CSV members; pass --member. "
                        f"members={len(csv_members)}, preview=[{preview}]"
                    )
                selected = csv_members[0]
            if selected not in archive.namelist():
                raise AlibabaTraceFormatError(
                    f"CSV member not found in zip: {selected}: {path}"
                )
            raw = archive.open(selected, mode="r")
            wrapper = TextIOWrapperCompat(raw)
            yield wrapper
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


class TextIOWrapperCompat:
    """Small wrapper kept local so zip members behave like newline-aware text files."""

    def __init__(self, raw: object) -> None:
        import io

        self._wrapper = io.TextIOWrapper(
            raw, encoding="utf-8-sig", newline=""  # type: ignore[arg-type]
        )

    def __iter__(self) -> "TextIOWrapperCompat":
        return self

    def __next__(self) -> str:
        return next(self._wrapper)

    def read(self, size: int = -1) -> str:
        return self._wrapper.read(size)

    def readline(self, size: int = -1) -> str:
        return self._wrapper.readline(size)

    def close(self) -> None:
        self._wrapper.close()


def iter_alibaba_ebs_rows(
    input_path: PathLike,
    *,
    member_name: Optional[str] = None,
) -> Iterator[AlibabaEBSRawIO]:
    path = _require_file(input_path, label="Alibaba EBS 2026 trace")
    with _open_ebs_text(path, member_name=member_name) as handle:
        reader = csv.DictReader(handle)
        normalized = [name.strip().lstrip("\ufeff") for name in (reader.fieldnames or [])]
        if not all(column in normalized for column in EBS_TRACE_COLUMNS):
            raise AlibabaTraceFormatError(
                f"Alibaba EBS trace requires columns {EBS_TRACE_COLUMNS}: "
                f"found={normalized}: {path}"
            )
        index = {column: normalized.index(column) for column in EBS_TRACE_COLUMNS}
        canonical_names = list(reader.fieldnames or [])

        previous_start: Optional[int] = None
        for row_number, row in enumerate(reader, start=2):
            if not row or all(not str(value or "").strip() for value in row.values()):
                continue
            values = [row.get(name, "") or "" for name in canonical_names]
            op_raw = _parse_int(
                values[index["op"]],
                column="op",
                row_number=row_number,
                path=path,
                minimum=1,
            )
            if op_raw not in {1, 2}:
                raise AlibabaTraceFormatError(
                    f"row {row_number}: op must be 1(read) or 2(write), got {op_raw}: {path}"
                )
            start_time = _parse_int(
                values[index["start_time"]],
                column="start_time",
                row_number=row_number,
                path=path,
            )
            end_time = _parse_int(
                values[index["end_time"]],
                column="end_time",
                row_number=row_number,
                path=path,
            )
            if end_time < start_time:
                raise AlibabaTraceFormatError(
                    f"row {row_number}: end_time {end_time} precedes start_time {start_time}: {path}"
                )
            if previous_start is not None and start_time < previous_start:
                raise AlibabaTraceFormatError(
                    f"row {row_number}: start_time decreased: {start_time} < {previous_start}: {path}"
                )
            previous_start = start_time

            yield AlibabaEBSRawIO(
                row_number=row_number,
                opcode="R" if op_raw == 1 else "W",
                offset_bytes=_parse_int(
                    values[index["dev_offset"]],
                    column="dev_offset",
                    row_number=row_number,
                    path=path,
                ),
                length_bytes=_parse_int(
                    values[index["length"]],
                    column="length",
                    row_number=row_number,
                    path=path,
                    minimum=1,
                ),
                start_time_us=start_time,
                end_time_us=end_time,
            )


def _page_span(offset_bytes: int, length_bytes: int, page_size: int) -> Tuple[int, int]:
    start_lpn = offset_bytes // page_size
    end_lpn_exclusive = math.ceil((offset_bytes + length_bytes) / page_size)
    return start_lpn, end_lpn_exclusive - start_lpn


def convert_alibaba_ebs_trace(
    input_path: PathLike,
    output_path: PathLike,
    *,
    page_size: int = 4096,
    max_rows: Optional[int] = None,
    skip_rows: int = 0,
    normalize_timestamps: bool = True,
    flow_id: Optional[str] = None,
    workload_type: Optional[str] = None,
    cluster_id: Optional[int] = None,
    volume_id: Optional[int] = None,
    member_name: Optional[str] = None,
    stats_path: Optional[PathLike] = None,
) -> AlibabaEBSConversionResult:
    if page_size <= 0:
        raise ValueError("page_size must be > 0")
    if max_rows is not None and max_rows <= 0:
        raise ValueError("max_rows must be > 0 when provided")
    if skip_rows < 0:
        raise ValueError("skip_rows must be >= 0")

    source = _require_file(input_path, label="Alibaba EBS 2026 trace")
    identity = infer_ebs_trace_identity(
        source,
        workload_type=workload_type,
        cluster_id=cluster_id,
        volume_id=volume_id,
        member_name=member_name,
    )
    selected_flow = flow_id or (
        f"alibaba_ebs_{identity.workload_type}_"
        f"cluster{identity.cluster_id}_dev{identity.volume_id}"
    )
    if not selected_flow.strip():
        raise ValueError("flow_id must not be empty")

    output = Path(output_path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    stats_output = (
        Path(stats_path).expanduser()
        if stats_path is not None
        else output.with_suffix(output.suffix + ".conversion.json")
    )
    stats_output.parent.mkdir(parents=True, exist_ok=True)

    source_rows = 0
    selected_rows = 0
    skipped = 0
    reads = writes = read_pages = write_pages = unaligned = 0
    timestamp_origin: Optional[int] = None
    min_ts: Optional[int] = None
    max_ts: Optional[int] = None
    min_lpn: Optional[int] = None
    max_lpn: Optional[int] = None
    latency_sum = 0
    latency_min: Optional[int] = None
    latency_max: Optional[int] = None
    stop_reason = "source_exhausted"

    try:
        with output.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=STANDARD_TRACE_COLUMNS)
            writer.writeheader()

            for raw in iter_alibaba_ebs_rows(source, member_name=member_name):
                source_rows += 1
                if source_rows <= skip_rows:
                    skipped += 1
                    continue
                if max_rows is not None and selected_rows >= max_rows:
                    stop_reason = "max_rows"
                    break

                lba, size_pages = _page_span(raw.offset_bytes, raw.length_bytes, page_size)
                if raw.offset_bytes % page_size or raw.length_bytes % page_size:
                    unaligned += 1
                if timestamp_origin is None:
                    timestamp_origin = raw.start_time_us if normalize_timestamps else 0
                timestamp_s = (raw.start_time_us - timestamp_origin) / 1_000_000.0

                writer.writerow(
                    {
                        "timestamp": f"{timestamp_s:.6f}",
                        "flow_id": selected_flow,
                        "op": raw.opcode,
                        "lba": lba,
                        "size": size_pages,
                    }
                )
                selected_rows += 1
                min_ts = raw.start_time_us if min_ts is None else min(min_ts, raw.start_time_us)
                max_ts = raw.start_time_us if max_ts is None else max(max_ts, raw.start_time_us)
                min_lpn = lba if min_lpn is None else min(min_lpn, lba)
                request_max_lpn = lba + size_pages - 1
                max_lpn = request_max_lpn if max_lpn is None else max(max_lpn, request_max_lpn)

                if raw.opcode == "R":
                    reads += 1
                    read_pages += size_pages
                else:
                    writes += 1
                    write_pages += size_pages

                observed = raw.observed_latency_us
                latency_sum += observed
                latency_min = observed if latency_min is None else min(latency_min, observed)
                latency_max = observed if latency_max is None else max(latency_max, observed)
    except Exception:
        output.unlink(missing_ok=True)
        raise

    if selected_rows == 0:
        output.unlink(missing_ok=True)
        raise AlibabaTraceFormatError(f"no rows selected from Alibaba EBS trace: {source}")

    assert timestamp_origin is not None
    assert min_ts is not None and max_ts is not None
    assert min_lpn is not None and max_lpn is not None
    assert latency_min is not None and latency_max is not None

    stats = AlibabaEBSConversionStats(
        source_rows=source_rows,
        selected_rows=selected_rows,
        skipped_rows=skipped,
        read_requests=reads,
        write_requests=writes,
        read_pages=read_pages,
        write_pages=write_pages,
        unaligned_requests=unaligned,
        page_size=page_size,
        min_raw_timestamp_us=min_ts,
        max_raw_timestamp_us=max_ts,
        min_lpn=min_lpn,
        max_lpn=max_lpn,
        flow_id=selected_flow,
        workload_type=identity.workload_type,
        cluster_id=identity.cluster_id,
        volume_id=identity.volume_id,
        normalized_timestamps=normalize_timestamps,
        stop_reason=stop_reason,
        observed_latency_avg_us=latency_sum / selected_rows,
        observed_latency_min_us=latency_min,
        observed_latency_max_us=latency_max,
    )

    payload = {
        "schema_version": 1,
        "source_format": "alibaba-cloud-ebs-io-traces-2026",
        "source_path": str(source.resolve()),
        "source_member": member_name,
        "output_standard_trace": str(output.resolve()),
        "trace_identity": identity.to_dict(),
        "selection_note": (
            "This conversion is a bounded per-volume experiment. Metadata file size "
            "is only an acquisition filter, not evidence of workload representativeness."
        ),
        "conversion_stats": stats.to_dict(),
    }
    with stats_output.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")

    return AlibabaEBSConversionResult(
        output_path=output,
        stats_path=stats_output,
        stats=stats,
    )
