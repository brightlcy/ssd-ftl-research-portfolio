from __future__ import annotations

import csv
import json
import math
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from .alibaba_trace import AlibabaRawIO, iter_alibaba_io_rows, load_alibaba_device_capacities


PathLike = Union[str, Path]
DEVICE_SUMMARY_COLUMNS = (
    "device_id",
    "capacity_bytes",
    "request_count",
    "read_requests",
    "write_requests",
    "read_ratio",
    "write_ratio",
    "read_pages",
    "write_pages",
    "avg_request_pages",
    "avg_read_pages",
    "avg_write_pages",
    "duration_s",
    "iops",
    "write_iops",
    "mean_interarrival_us",
    "short_interarrival_ratio",
    "sequential_request_ratio",
    "write_pattern",
    "size_pattern",
)
CANDIDATE_COLUMNS = (
    "candidate_role",
    "device_id",
    "request_count",
    "write_ratio",
    "avg_write_pages",
    "short_interarrival_ratio",
    "sequential_request_ratio",
    "selection_reason",
)


@dataclass
class _DeviceAccumulator:
    device_id: int
    capacity_bytes: Optional[int] = None
    request_count: int = 0
    read_requests: int = 0
    write_requests: int = 0
    read_pages: int = 0
    write_pages: int = 0
    first_timestamp_us: Optional[int] = None
    last_timestamp_us: Optional[int] = None
    previous_timestamp_us: Optional[int] = None
    interarrival_count: int = 0
    interarrival_sum_us: int = 0
    short_interarrival_count: int = 0
    previous_end_offset: Optional[int] = None
    transition_count: int = 0
    sequential_transition_count: int = 0

    def observe(self, row: AlibabaRawIO, *, page_size: int, short_iat_us: int) -> None:
        pages = math.ceil((row.offset_bytes % page_size + row.length_bytes) / page_size)
        self.request_count += 1
        if row.opcode == "R":
            self.read_requests += 1
            self.read_pages += pages
        else:
            self.write_requests += 1
            self.write_pages += pages

        if self.first_timestamp_us is None:
            self.first_timestamp_us = row.timestamp_us
        self.last_timestamp_us = row.timestamp_us

        if self.previous_timestamp_us is not None:
            delta = row.timestamp_us - self.previous_timestamp_us
            if delta < 0:
                raise ValueError(
                    f"device {self.device_id} timestamp decreased: "
                    f"{row.timestamp_us} < {self.previous_timestamp_us}"
                )
            self.interarrival_count += 1
            self.interarrival_sum_us += delta
            if delta <= short_iat_us:
                self.short_interarrival_count += 1
        self.previous_timestamp_us = row.timestamp_us

        if self.previous_end_offset is not None:
            self.transition_count += 1
            if row.offset_bytes == self.previous_end_offset:
                self.sequential_transition_count += 1
        self.previous_end_offset = row.offset_bytes + row.length_bytes


@dataclass(frozen=True)
class AlibabaDeviceSummary:
    device_id: int
    capacity_bytes: int
    request_count: int
    read_requests: int
    write_requests: int
    read_ratio: float
    write_ratio: float
    read_pages: int
    write_pages: int
    avg_request_pages: float
    avg_read_pages: float
    avg_write_pages: float
    duration_s: float
    iops: float
    write_iops: float
    mean_interarrival_us: float
    short_interarrival_ratio: float
    sequential_request_ratio: float
    write_pattern: str
    size_pattern: str

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class AlibabaCandidate:
    candidate_role: str
    device_id: int
    request_count: int
    write_ratio: float
    avg_write_pages: float
    short_interarrival_ratio: float
    sequential_request_ratio: float
    selection_reason: str

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class AlibabaAnalysisResult:
    source_path: Path
    device_summary_path: Path
    candidate_path: Path
    dataset_summary_path: Path
    source_rows: int
    analyzed_devices: int
    candidates: Tuple[AlibabaCandidate, ...]
    analyzed_duration_s: float
    stop_reason: str


def _classify_write_pattern(write_ratio: float) -> str:
    if write_ratio >= 0.70:
        return "write_intensive"
    if write_ratio <= 0.30:
        return "read_intensive"
    return "mixed"


def _classify_size(avg_pages: float) -> str:
    if avg_pages <= 2.0:
        return "small"
    if avg_pages >= 8.0:
        return "large"
    return "medium"


def _finalize(acc: _DeviceAccumulator) -> AlibabaDeviceSummary:
    assert acc.first_timestamp_us is not None
    assert acc.last_timestamp_us is not None
    duration_s = max(0.0, (acc.last_timestamp_us - acc.first_timestamp_us) / 1_000_000.0)
    total_pages = acc.read_pages + acc.write_pages
    read_ratio = acc.read_requests / acc.request_count
    write_ratio = acc.write_requests / acc.request_count
    avg_request_pages = total_pages / acc.request_count
    avg_read_pages = acc.read_pages / acc.read_requests if acc.read_requests else 0.0
    avg_write_pages = acc.write_pages / acc.write_requests if acc.write_requests else 0.0
    iops = acc.request_count / duration_s if duration_s > 0 else 0.0
    write_iops = acc.write_requests / duration_s if duration_s > 0 else 0.0
    mean_iat = (
        acc.interarrival_sum_us / acc.interarrival_count
        if acc.interarrival_count
        else 0.0
    )
    short_ratio = (
        acc.short_interarrival_count / acc.interarrival_count
        if acc.interarrival_count
        else 0.0
    )
    sequential_ratio = (
        acc.sequential_transition_count / acc.transition_count
        if acc.transition_count
        else 0.0
    )
    return AlibabaDeviceSummary(
        device_id=acc.device_id,
        capacity_bytes=acc.capacity_bytes or 0,
        request_count=acc.request_count,
        read_requests=acc.read_requests,
        write_requests=acc.write_requests,
        read_ratio=read_ratio,
        write_ratio=write_ratio,
        read_pages=acc.read_pages,
        write_pages=acc.write_pages,
        avg_request_pages=avg_request_pages,
        avg_read_pages=avg_read_pages,
        avg_write_pages=avg_write_pages,
        duration_s=duration_s,
        iops=iops,
        write_iops=write_iops,
        mean_interarrival_us=mean_iat,
        short_interarrival_ratio=short_ratio,
        sequential_request_ratio=sequential_ratio,
        write_pattern=_classify_write_pattern(write_ratio),
        size_pattern=_classify_size(avg_request_pages),
    )


def select_representative_candidates(
    summaries: Sequence[AlibabaDeviceSummary],
    *,
    minimum_requests: int = 1000,
) -> Tuple[AlibabaCandidate, ...]:
    """Choose deterministic workload roles without pretending one volume is representative.

    The roles are deliberately descriptive rather than statistical clusters. They are
    used to seed bounded experiments; later analysis may replace them with clustering.
    """
    eligible = [item for item in summaries if item.request_count >= minimum_requests]
    if not eligible:
        eligible = list(summaries)
    if not eligible:
        return ()

    selected: List[AlibabaCandidate] = []
    used: set[int] = set()

    def pick(role: str, ordered: Iterable[AlibabaDeviceSummary], reason: str) -> None:
        for item in ordered:
            if item.device_id in used:
                continue
            used.add(item.device_id)
            selected.append(
                AlibabaCandidate(
                    candidate_role=role,
                    device_id=item.device_id,
                    request_count=item.request_count,
                    write_ratio=item.write_ratio,
                    avg_write_pages=item.avg_write_pages,
                    short_interarrival_ratio=item.short_interarrival_ratio,
                    sequential_request_ratio=item.sequential_request_ratio,
                    selection_reason=reason,
                )
            )
            return

    pick(
        "write_intensive",
        sorted(eligible, key=lambda x: (-x.write_ratio, -x.write_requests, x.device_id)),
        "highest write ratio, then highest write request count",
    )
    pick(
        "read_intensive",
        sorted(eligible, key=lambda x: (x.write_ratio, -x.read_requests, x.device_id)),
        "lowest write ratio, then highest read request count",
    )
    pick(
        "mixed",
        sorted(eligible, key=lambda x: (abs(x.write_ratio - 0.5), -x.request_count, x.device_id)),
        "write ratio closest to 0.5",
    )
    pick(
        "high_burst",
        sorted(eligible, key=lambda x: (-x.short_interarrival_ratio, -x.iops, x.device_id)),
        "highest fraction of per-device inter-arrivals within the burst threshold",
    )
    pick(
        "small_random_write",
        sorted(
            eligible,
            key=lambda x: (
                -(
                    x.write_ratio
                    * (1.0 - x.sequential_request_ratio)
                    / max(x.avg_write_pages, 0.25)
                ),
                -x.write_requests,
                x.device_id,
            ),
        ),
        "high write ratio, low sequentiality, and small average write size",
    )
    return tuple(selected)


def _atomic_write_text(path: Path, text: str) -> None:
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(str(temp), str(path))


def _snapshot_analysis(
    *,
    source: Path,
    out: Path,
    accumulators: Mapping[int, _DeviceAccumulator],
    source_rows: int,
    source_first_timestamp_us: Optional[int],
    source_last_timestamp_us: Optional[int],
    page_size: int,
    short_iat_us: int,
    max_source_rows: Optional[int],
    max_duration_seconds: Optional[float],
    minimum_candidate_requests: int,
    stop_reason: str,
    elapsed_wall_s: float,
    checkpoint: bool,
) -> Tuple[Path, Path, Path, Tuple[AlibabaDeviceSummary, ...], Tuple[AlibabaCandidate, ...], float]:
    summaries = tuple(
        _finalize(accumulators[device_id]) for device_id in sorted(accumulators)
    )
    if not summaries:
        raise ValueError(f"no Alibaba I/O rows were analyzed: {source}")
    candidates = select_representative_candidates(
        summaries, minimum_requests=minimum_candidate_requests
    )

    prefix = "checkpoint_" if checkpoint else ""
    device_summary_path = out / f"{prefix}alibaba_device_summary.csv"
    candidate_path = out / f"{prefix}representative_candidates.csv"
    dataset_summary_path = out / f"{prefix}alibaba_dataset_summary.json"

    device_lines = []
    from io import StringIO
    device_buffer = StringIO()
    writer = csv.DictWriter(device_buffer, fieldnames=DEVICE_SUMMARY_COLUMNS)
    writer.writeheader()
    for item in summaries:
        writer.writerow(item.to_dict())
    _atomic_write_text(device_summary_path, device_buffer.getvalue())

    candidate_buffer = StringIO()
    writer = csv.DictWriter(candidate_buffer, fieldnames=CANDIDATE_COLUMNS)
    writer.writeheader()
    for item in candidates:
        writer.writerow(item.to_dict())
    _atomic_write_text(candidate_path, candidate_buffer.getvalue())

    total_requests = sum(item.request_count for item in summaries)
    total_reads = sum(item.read_requests for item in summaries)
    total_writes = sum(item.write_requests for item in summaries)
    total_read_pages = sum(item.read_pages for item in summaries)
    total_write_pages = sum(item.write_pages for item in summaries)
    analyzed_duration_s = (
        0.0
        if source_first_timestamp_us is None or source_last_timestamp_us is None
        else (source_last_timestamp_us - source_first_timestamp_us) / 1_000_000.0
    )
    rows_per_second = source_rows / elapsed_wall_s if elapsed_wall_s > 0 else 0.0
    payload = {
        "schema_version": 2,
        "source_path": "<stdin>" if str(source) == "-" else str(source.resolve()),
        "analysis_mode": "streaming O(number_of_devices) memory",
        "source_rows": source_rows,
        "analyzed_devices": len(summaries),
        "page_size": page_size,
        "short_interarrival_threshold_us": short_iat_us,
        "max_source_rows": max_source_rows,
        "max_duration_seconds": max_duration_seconds,
        "analyzed_duration_s": analyzed_duration_s,
        "wall_elapsed_s": elapsed_wall_s,
        "rows_per_second": rows_per_second,
        "stop_reason": stop_reason,
        "is_checkpoint": checkpoint,
        "totals": {
            "requests": total_requests,
            "reads": total_reads,
            "writes": total_writes,
            "write_ratio": total_writes / total_requests,
            "read_pages": total_read_pages,
            "write_pages": total_write_pages,
        },
        "candidate_roles": [item.to_dict() for item in candidates],
        "methodological_note": (
            "The Alibaba corpus is scanned as a decompressed stream. Pilot runs use a "
            "bounded row budget and periodic atomic snapshots so progress is visible and "
            "partial statistics survive interruption. FTL simulations still use bounded "
            "per-device slices."
        ),
    }
    _atomic_write_text(
        dataset_summary_path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return (
        device_summary_path,
        candidate_path,
        dataset_summary_path,
        summaries,
        candidates,
        analyzed_duration_s,
    )


def analyze_alibaba_workloads(
    input_path: PathLike,
    output_dir: PathLike,
    *,
    device_size_path: Optional[PathLike] = None,
    page_size: int = 4096,
    short_iat_us: int = 1000,
    max_source_rows: Optional[int] = None,
    max_duration_seconds: Optional[float] = None,
    minimum_candidate_requests: int = 1000,
    progress_every_rows: Optional[int] = None,
    checkpoint_every_rows: Optional[int] = None,
    progress_callback: Optional[Callable[[Mapping[str, object]], None]] = None,
) -> AlibabaAnalysisResult:
    if page_size <= 0:
        raise ValueError("page_size must be > 0")
    if short_iat_us < 0:
        raise ValueError("short_iat_us must be >= 0")
    if max_source_rows is not None and max_source_rows <= 0:
        raise ValueError("max_source_rows must be > 0 when provided")
    if max_duration_seconds is not None and max_duration_seconds <= 0:
        raise ValueError("max_duration_seconds must be > 0 when provided")
    if minimum_candidate_requests <= 0:
        raise ValueError("minimum_candidate_requests must be > 0")
    if progress_every_rows is not None and progress_every_rows <= 0:
        raise ValueError("progress_every_rows must be > 0 when provided")
    if checkpoint_every_rows is not None and checkpoint_every_rows <= 0:
        raise ValueError("checkpoint_every_rows must be > 0 when provided")

    source = Path(input_path).expanduser()
    out = Path(output_dir).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    capacities: Mapping[int, int] = {}
    if device_size_path is not None:
        capacities = load_alibaba_device_capacities(device_size_path)

    accumulators: Dict[int, _DeviceAccumulator] = {}
    source_rows = 0
    source_first_timestamp_us: Optional[int] = None
    source_last_timestamp_us: Optional[int] = None
    previous_source_timestamp_us: Optional[int] = None
    stop_reason = "source_exhausted"
    started = time.perf_counter()

    def notify_progress(*, checkpoint_written: bool = False) -> None:
        if progress_callback is None:
            return
        elapsed = max(0.0, time.perf_counter() - started)
        rate = source_rows / elapsed if elapsed > 0 else 0.0
        remaining_rows = None
        eta_s = None
        if max_source_rows is not None:
            remaining_rows = max(0, max_source_rows - source_rows)
            if rate > 0:
                eta_s = remaining_rows / rate
        trace_duration_s = (
            0.0
            if source_first_timestamp_us is None or source_last_timestamp_us is None
            else (source_last_timestamp_us - source_first_timestamp_us) / 1_000_000.0
        )
        progress_callback({
            "source_rows": source_rows,
            "analyzed_devices": len(accumulators),
            "wall_elapsed_s": elapsed,
            "rows_per_second": rate,
            "remaining_rows": remaining_rows,
            "eta_s": eta_s,
            "trace_duration_s": trace_duration_s,
            "checkpoint_written": checkpoint_written,
        })

    try:
        for row in iter_alibaba_io_rows(source):
            if (
                max_duration_seconds is not None
                and previous_source_timestamp_us is not None
                and row.timestamp_us < previous_source_timestamp_us
            ):
                raise ValueError(
                    "Alibaba source timestamps must be globally non-decreasing for "
                    "duration-bounded streaming analysis"
                )
            previous_source_timestamp_us = row.timestamp_us
            if source_first_timestamp_us is None:
                source_first_timestamp_us = row.timestamp_us
            if (
                max_duration_seconds is not None
                and row.timestamp_us - source_first_timestamp_us
                >= int(max_duration_seconds * 1_000_000)
            ):
                stop_reason = "max_duration_seconds"
                break
            if max_source_rows is not None and source_rows >= max_source_rows:
                stop_reason = "max_source_rows"
                break

            source_rows += 1
            source_last_timestamp_us = row.timestamp_us
            acc = accumulators.get(row.device_id)
            if acc is None:
                acc = _DeviceAccumulator(
                    device_id=row.device_id,
                    capacity_bytes=capacities.get(row.device_id),
                )
                accumulators[row.device_id] = acc
            acc.observe(row, page_size=page_size, short_iat_us=short_iat_us)

            checkpoint_written = False
            if (
                checkpoint_every_rows is not None
                and source_rows % checkpoint_every_rows == 0
            ):
                elapsed = max(0.0, time.perf_counter() - started)
                _snapshot_analysis(
                    source=source,
                    out=out,
                    accumulators=accumulators,
                    source_rows=source_rows,
                    source_first_timestamp_us=source_first_timestamp_us,
                    source_last_timestamp_us=source_last_timestamp_us,
                    page_size=page_size,
                    short_iat_us=short_iat_us,
                    max_source_rows=max_source_rows,
                    max_duration_seconds=max_duration_seconds,
                    minimum_candidate_requests=minimum_candidate_requests,
                    stop_reason="checkpoint",
                    elapsed_wall_s=elapsed,
                    checkpoint=True,
                )
                checkpoint_written = True

            if (
                progress_every_rows is not None
                and source_rows % progress_every_rows == 0
            ):
                notify_progress(checkpoint_written=checkpoint_written)
    except KeyboardInterrupt:
        stop_reason = "interrupted"

    elapsed_wall_s = max(0.0, time.perf_counter() - started)
    (
        device_summary_path,
        candidate_path,
        dataset_summary_path,
        summaries,
        candidates,
        analyzed_duration_s,
    ) = _snapshot_analysis(
        source=source,
        out=out,
        accumulators=accumulators,
        source_rows=source_rows,
        source_first_timestamp_us=source_first_timestamp_us,
        source_last_timestamp_us=source_last_timestamp_us,
        page_size=page_size,
        short_iat_us=short_iat_us,
        max_source_rows=max_source_rows,
        max_duration_seconds=max_duration_seconds,
        minimum_candidate_requests=minimum_candidate_requests,
        stop_reason=stop_reason,
        elapsed_wall_s=elapsed_wall_s,
        checkpoint=False,
    )
    notify_progress(checkpoint_written=False)

    return AlibabaAnalysisResult(
        source_path=source,
        device_summary_path=device_summary_path,
        candidate_path=candidate_path,
        dataset_summary_path=dataset_summary_path,
        source_rows=source_rows,
        analyzed_devices=len(summaries),
        candidates=candidates,
        analyzed_duration_s=analyzed_duration_s,
        stop_reason=stop_reason,
    )

