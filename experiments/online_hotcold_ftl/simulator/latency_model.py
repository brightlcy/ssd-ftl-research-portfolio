from __future__ import annotations

import csv
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Union

from .baseline_ftl import BaselineFTL, FTLCounterSnapshot
from .trace_io import iter_standard_trace


PathLike = Union[str, Path]
TraceRow = Mapping[str, Any]


class LatencyModelError(RuntimeError):
    """Raised when request timing cannot be modeled consistently."""


@dataclass(frozen=True)
class LatencyModelConfig:
    """Deterministic service-time constants for the Step 09 timing model.

    This is intentionally a simple single-resource analytical model. It is not
    a calibrated NAND/device timing simulator. The constants convert functional
    FTL events into comparable latency costs so GC-affected tail behavior can be
    measured before channel/chip scheduling is introduced in a later step.
    """

    command_overhead_us: float = 20.0
    read_page_us: float = 80.0
    program_page_us: float = 200.0
    erase_block_us: float = 2000.0

    def __post_init__(self) -> None:
        values = {
            "command_overhead_us": self.command_overhead_us,
            "read_page_us": self.read_page_us,
            "program_page_us": self.program_page_us,
            "erase_block_us": self.erase_block_us,
        }
        for name, value in values.items():
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite value >= 0: {value}")
        if self.command_overhead_us == 0 and all(
            value == 0
            for value in (
                self.read_page_us,
                self.program_page_us,
                self.erase_block_us,
            )
        ):
            raise ValueError("latency model cannot have every cost equal to zero")

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data.update(
            {
                "model_name": "single-resource-event-cost-v1",
                "queue_model": "FCFS single serialized resource",
                "percentile_method": "nearest-rank",
                "calibration_status": "analytical-not-hardware-calibrated",
            }
        )
        return data


@dataclass(frozen=True)
class RequestLatencyEntry:
    """One request's modeled queueing, service, and response latency."""

    request_id: int
    round_index: int
    row_index: int
    trace_timestamp_s: float
    arrival_time_us: float
    flow_id: str
    op: str
    lba: int
    size_pages: int
    host_write_pages: int
    nand_write_pages: int
    migrated_pages: int
    gc_count: int
    gc_affected: bool
    queue_delay_us: float
    service_time_us: float
    latency_us: float
    completion_time_us: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


REQUEST_LATENCY_FIELDS = (
    "request_id",
    "round_index",
    "row_index",
    "trace_timestamp_s",
    "arrival_time_us",
    "flow_id",
    "op",
    "lba",
    "size_pages",
    "host_write_pages",
    "nand_write_pages",
    "migrated_pages",
    "gc_count",
    "gc_affected",
    "queue_delay_us",
    "service_time_us",
    "latency_us",
    "completion_time_us",
)

LATENCY_SUMMARY_FIELDS = (
    "scope",
    "request_count",
    "gc_affected_requests",
    "avg_service_time_us",
    "avg_queue_delay_us",
    "avg_latency_us",
    "p50_latency_us",
    "p95_latency_us",
    "p99_latency_us",
    "p99_9_latency_us",
    "max_latency_us",
)


def _counter_delta(
    before: FTLCounterSnapshot,
    after: FTLCounterSnapshot,
    field: str,
) -> int:
    value = getattr(after, field) - getattr(before, field)
    if value < 0:
        raise LatencyModelError(f"counter moved backwards while timing request: {field}")
    return value


def nearest_rank_percentile(values: Sequence[float], percentile: float) -> float:
    """Return a deterministic nearest-rank percentile.

    The nearest-rank definition selects rank ceil(p/100 * N), with ranks starting
    at one. Empty inputs return 0.0 so empty operation scopes remain serializable.
    """
    if not 0.0 <= percentile <= 100.0:
        raise ValueError(f"percentile must be between 0 and 100: {percentile}")
    if not values:
        return 0.0

    ordered = sorted(float(value) for value in values)
    if percentile == 0:
        return ordered[0]
    rank = max(1, math.ceil((percentile / 100.0) * len(ordered)))
    return ordered[rank - 1]


class RequestLatencyRecorder:
    """Run measurement requests through an FTL and build modeled latency logs.

    Requests are scheduled FCFS on one serialized resource. Trace timestamps are
    interpreted as seconds. Each replay round starts after the prior round's last
    completion, while preserving the inter-arrival pattern inside the trace.
    """

    def __init__(self, model: LatencyModelConfig) -> None:
        self.model = model
        self.entries: List[RequestLatencyEntry] = []
        self.device_available_us = 0.0
        self._next_request_id = 1

    def _service_time_us(
        self,
        *,
        op: str,
        size_pages: int,
        host_write_pages: int,
        migrated_pages: int,
        gc_count: int,
    ) -> float:
        service = self.model.command_overhead_us
        if op == "R":
            service += size_pages * self.model.read_page_us
        elif op == "W":
            service += host_write_pages * self.model.program_page_us
            service += migrated_pages * (
                self.model.read_page_us + self.model.program_page_us
            )
            service += gc_count * self.model.erase_block_us
        else:
            raise LatencyModelError(f"unsupported operation for timing: {op!r}")
        return service

    def record_row(
        self,
        ftl: BaselineFTL,
        row: TraceRow,
        *,
        round_index: int,
        row_index: int,
        arrival_time_us: float,
    ) -> RequestLatencyEntry:
        if round_index <= 0:
            raise ValueError(f"round_index must be > 0: {round_index}")
        if row_index <= 0:
            raise ValueError(f"row_index must be > 0: {row_index}")
        if arrival_time_us < 0 or not math.isfinite(arrival_time_us):
            raise ValueError(
                f"arrival_time_us must be finite and >= 0: {arrival_time_us}"
            )

        op = str(row["op"]).strip().upper()
        size_pages = int(row["size"])
        before = ftl.capture_counter_snapshot()
        ftl.process_trace_row(row)
        after = ftl.capture_counter_snapshot()

        host_write_pages = _counter_delta(before, after, "host_write_pages")
        nand_write_pages = _counter_delta(before, after, "nand_write_pages")
        migrated_pages = _counter_delta(before, after, "valid_page_migrations")
        gc_count = _counter_delta(before, after, "gc_count")

        service_time_us = self._service_time_us(
            op=op,
            size_pages=size_pages,
            host_write_pages=host_write_pages,
            migrated_pages=migrated_pages,
            gc_count=gc_count,
        )
        start_time_us = max(arrival_time_us, self.device_available_us)
        queue_delay_us = start_time_us - arrival_time_us
        completion_time_us = start_time_us + service_time_us
        latency_us = completion_time_us - arrival_time_us
        self.device_available_us = completion_time_us

        entry = RequestLatencyEntry(
            request_id=self._next_request_id,
            round_index=round_index,
            row_index=row_index,
            trace_timestamp_s=float(row["timestamp"]),
            arrival_time_us=arrival_time_us,
            flow_id=str(row["flow_id"]),
            op=op,
            lba=int(row["lba"]),
            size_pages=size_pages,
            host_write_pages=host_write_pages,
            nand_write_pages=nand_write_pages,
            migrated_pages=migrated_pages,
            gc_count=gc_count,
            gc_affected=gc_count > 0,
            queue_delay_us=queue_delay_us,
            service_time_us=service_time_us,
            latency_us=latency_us,
            completion_time_us=completion_time_us,
        )
        self.entries.append(entry)
        self._next_request_id += 1
        return entry

    def run_trace_round(
        self,
        ftl: BaselineFTL,
        input_path: PathLike,
        *,
        round_index: int,
        trace_min_timestamp: float,
    ) -> List[RequestLatencyEntry]:
        """Replay one trace round and return only entries created in that round."""
        if round_index <= 0:
            raise ValueError(f"round_index must be > 0: {round_index}")
        if trace_min_timestamp < 0 or not math.isfinite(trace_min_timestamp):
            raise ValueError(
                "trace_min_timestamp must be finite and >= 0: "
                f"{trace_min_timestamp}"
            )

        start_index = len(self.entries)
        round_start_us = self.device_available_us
        previous_timestamp = None

        for row_index, row in enumerate(iter_standard_trace(input_path), start=1):
            timestamp = float(row["timestamp"])
            if previous_timestamp is not None and timestamp < previous_timestamp:
                raise LatencyModelError(
                    "latency modeling requires nondecreasing timestamps: "
                    f"round={round_index}, row={row_index}, "
                    f"previous={previous_timestamp}, current={timestamp}"
                )
            previous_timestamp = timestamp
            relative_arrival_us = (timestamp - trace_min_timestamp) * 1_000_000.0
            if relative_arrival_us < -1e-9:
                raise LatencyModelError(
                    "trace timestamp precedes the resolved minimum timestamp: "
                    f"{timestamp} < {trace_min_timestamp}"
                )
            arrival_time_us = round_start_us + max(0.0, relative_arrival_us)
            self.record_row(
                ftl,
                row,
                round_index=round_index,
                row_index=row_index,
                arrival_time_us=arrival_time_us,
            )

        return self.entries[start_index:]


def _average(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _build_scope_row(
    scope: str,
    entries: Sequence[RequestLatencyEntry],
) -> Dict[str, Any]:
    latencies = [entry.latency_us for entry in entries]
    queue_delays = [entry.queue_delay_us for entry in entries]
    service_times = [entry.service_time_us for entry in entries]
    return {
        "scope": scope,
        "request_count": len(entries),
        "gc_affected_requests": sum(1 for entry in entries if entry.gc_affected),
        "avg_service_time_us": _average(service_times),
        "avg_queue_delay_us": _average(queue_delays),
        "avg_latency_us": _average(latencies),
        "p50_latency_us": nearest_rank_percentile(latencies, 50.0),
        "p95_latency_us": nearest_rank_percentile(latencies, 95.0),
        "p99_latency_us": nearest_rank_percentile(latencies, 99.0),
        "p99_9_latency_us": nearest_rank_percentile(latencies, 99.9),
        "max_latency_us": max(latencies) if latencies else 0.0,
    }


def build_latency_summary(
    entries: Sequence[RequestLatencyEntry],
) -> List[Dict[str, Any]]:
    """Build all/read/write/GC-write latency percentile rows."""
    reads = [entry for entry in entries if entry.op == "R"]
    writes = [entry for entry in entries if entry.op == "W"]
    gc_writes = [entry for entry in writes if entry.gc_affected]
    non_gc_writes = [entry for entry in writes if not entry.gc_affected]
    return [
        _build_scope_row("all", entries),
        _build_scope_row("read", reads),
        _build_scope_row("write", writes),
        _build_scope_row("write_gc_affected", gc_writes),
        _build_scope_row("write_without_gc", non_gc_writes),
    ]


def latency_summary_by_scope(
    rows: Sequence[Mapping[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        scope = str(row["scope"])
        result[scope] = dict(row)
    return result


def _prepare_output_path(output_dir: PathLike, filename: str) -> Path:
    if not filename or not filename.strip():
        raise ValueError("filename must not be empty")
    directory = Path(output_dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    return directory / filename


def save_request_latency_log(
    output_dir: PathLike,
    entries: Iterable[RequestLatencyEntry],
    *,
    filename: str = "request_latency.csv",
) -> Path:
    path = _prepare_output_path(output_dir, filename)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REQUEST_LATENCY_FIELDS)
        writer.writeheader()
        for entry in entries:
            writer.writerow(entry.to_dict())
    return path


def save_latency_summary(
    output_dir: PathLike,
    rows: Sequence[Mapping[str, Any]],
    *,
    filename: str = "latency_summary.csv",
) -> Path:
    path = _prepare_output_path(output_dir, filename)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=LATENCY_SUMMARY_FIELDS)
        writer.writeheader()
        for row in rows:
            if set(row.keys()) != set(LATENCY_SUMMARY_FIELDS):
                raise ValueError(
                    "latency summary row schema mismatch: "
                    f"expected={LATENCY_SUMMARY_FIELDS}, got={tuple(row.keys())}"
                )
            writer.writerow(dict(row))
    return path


def latency_sanity_checks(
    entries: Sequence[RequestLatencyEntry],
    *,
    expected_requests: int,
    measurement_summary: Mapping[str, Any],
) -> Dict[str, bool]:
    """Cross-check request timing records against measurement FTL counters."""
    return {
        "request_count_identity": len(entries) == expected_requests,
        "read_request_identity": sum(1 for entry in entries if entry.op == "R")
        == measurement_summary["host_read_requests"],
        "write_request_identity": sum(1 for entry in entries if entry.op == "W")
        == measurement_summary["host_write_requests"],
        "nand_write_identity": sum(entry.nand_write_pages for entry in entries)
        == measurement_summary["nand_write_pages"],
        "migration_identity": sum(entry.migrated_pages for entry in entries)
        == measurement_summary["valid_page_migrations"],
        "gc_count_identity": sum(entry.gc_count for entry in entries)
        == measurement_summary["gc_count"],
        "latency_nonnegative": all(
            entry.queue_delay_us >= 0
            and entry.service_time_us >= 0
            and entry.latency_us >= entry.service_time_us
            and entry.completion_time_us >= entry.arrival_time_us
            for entry in entries
        ),
    }
