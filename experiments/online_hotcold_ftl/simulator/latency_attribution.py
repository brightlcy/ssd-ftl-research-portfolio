from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Union

from .latency_model import RequestLatencyEntry, nearest_rank_percentile


PathLike = Union[str, Path]

CAUSE_DIRECT_GC = "direct_gc"
CAUSE_GC_SPILLOVER = "gc_spillover"
CAUSE_WORKLOAD_QUEUE = "workload_queue"
CAUSE_UNQUEUED = "unqueued"
CAUSE_ORDER = (
    CAUSE_DIRECT_GC,
    CAUSE_GC_SPILLOVER,
    CAUSE_WORKLOAD_QUEUE,
    CAUSE_UNQUEUED,
)


@dataclass(frozen=True)
class LatencyAttributionEntry:
    """One request labeled by the source of its modeled latency.

    The single-resource model forms a busy chain whenever requests arrive before
    the device becomes idle. A GC request is a direct GC event. Later non-GC
    requests in the same undrained busy chain are labeled GC spillover because
    the GC service contributed to the backlog they inherit.
    """

    request_id: int
    round_index: int
    row_index: int
    flow_id: str
    op: str
    gc_affected: bool
    latency_cause: str
    causing_gc_request_id: Optional[int]
    busy_chain_id: int
    queue_delay_us: float
    service_time_us: float
    latency_us: float
    queue_fraction: float
    service_fraction: float

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        if self.causing_gc_request_id is None:
            data["causing_gc_request_id"] = ""
        return data


ATTRIBUTION_FIELDS = (
    "request_id",
    "round_index",
    "row_index",
    "flow_id",
    "op",
    "gc_affected",
    "latency_cause",
    "causing_gc_request_id",
    "busy_chain_id",
    "queue_delay_us",
    "service_time_us",
    "latency_us",
    "queue_fraction",
    "service_fraction",
)

CAUSE_SUMMARY_FIELDS = (
    "latency_cause",
    "request_count",
    "read_requests",
    "write_requests",
    "avg_queue_delay_us",
    "avg_service_time_us",
    "avg_latency_us",
    "queue_share_percent",
    "p50_latency_us",
    "p95_latency_us",
    "p99_latency_us",
    "p99_9_latency_us",
    "max_latency_us",
)

TAIL_SUMMARY_FIELDS = (
    "tail_scope",
    "latency_threshold_us",
    "request_count",
    "direct_gc_requests",
    "gc_spillover_requests",
    "workload_queue_requests",
    "unqueued_requests",
    "gc_related_requests",
    "gc_related_ratio",
)


def attribute_latency_causes(
    entries: Sequence[RequestLatencyEntry],
    *,
    epsilon_us: float = 1e-9,
) -> List[LatencyAttributionEntry]:
    """Classify direct GC, GC spillover, ordinary queueing, and unqueued work."""
    if epsilon_us < 0:
        raise ValueError(f"epsilon_us must be >= 0: {epsilon_us}")

    result: List[LatencyAttributionEntry] = []
    busy_chain_id = 0
    chain_has_gc = False
    latest_gc_request_id: Optional[int] = None

    for entry in entries:
        queued = entry.queue_delay_us > epsilon_us
        if not queued:
            busy_chain_id += 1
            chain_has_gc = False
            latest_gc_request_id = None

        if entry.gc_affected:
            cause = CAUSE_DIRECT_GC
            chain_has_gc = True
            latest_gc_request_id = entry.request_id
            causing_gc_request_id = entry.request_id
        elif queued and chain_has_gc:
            cause = CAUSE_GC_SPILLOVER
            causing_gc_request_id = latest_gc_request_id
        elif queued:
            cause = CAUSE_WORKLOAD_QUEUE
            causing_gc_request_id = None
        else:
            cause = CAUSE_UNQUEUED
            causing_gc_request_id = None

        latency = float(entry.latency_us)
        queue_fraction = entry.queue_delay_us / latency if latency > 0 else 0.0
        service_fraction = entry.service_time_us / latency if latency > 0 else 0.0

        result.append(
            LatencyAttributionEntry(
                request_id=entry.request_id,
                round_index=entry.round_index,
                row_index=entry.row_index,
                flow_id=entry.flow_id,
                op=entry.op,
                gc_affected=entry.gc_affected,
                latency_cause=cause,
                causing_gc_request_id=causing_gc_request_id,
                busy_chain_id=busy_chain_id,
                queue_delay_us=entry.queue_delay_us,
                service_time_us=entry.service_time_us,
                latency_us=entry.latency_us,
                queue_fraction=queue_fraction,
                service_fraction=service_fraction,
            )
        )

    return result


def _average(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _cause_summary_row(
    cause: str,
    entries: Sequence[LatencyAttributionEntry],
) -> Dict[str, Any]:
    latencies = [entry.latency_us for entry in entries]
    queue_delays = [entry.queue_delay_us for entry in entries]
    service_times = [entry.service_time_us for entry in entries]
    total_latency = sum(latencies)
    total_queue = sum(queue_delays)
    return {
        "latency_cause": cause,
        "request_count": len(entries),
        "read_requests": sum(1 for entry in entries if entry.op == "R"),
        "write_requests": sum(1 for entry in entries if entry.op == "W"),
        "avg_queue_delay_us": _average(queue_delays),
        "avg_service_time_us": _average(service_times),
        "avg_latency_us": _average(latencies),
        "queue_share_percent": (
            (total_queue / total_latency) * 100.0 if total_latency > 0 else 0.0
        ),
        "p50_latency_us": nearest_rank_percentile(latencies, 50.0),
        "p95_latency_us": nearest_rank_percentile(latencies, 95.0),
        "p99_latency_us": nearest_rank_percentile(latencies, 99.0),
        "p99_9_latency_us": nearest_rank_percentile(latencies, 99.9),
        "max_latency_us": max(latencies) if latencies else 0.0,
    }


def build_cause_summary(
    entries: Sequence[LatencyAttributionEntry],
) -> List[Dict[str, Any]]:
    """Return stable cause rows, including zero-count categories."""
    return [
        _cause_summary_row(
            cause,
            [entry for entry in entries if entry.latency_cause == cause],
        )
        for cause in CAUSE_ORDER
    ]


def cause_summary_by_name(
    rows: Sequence[Mapping[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    return {str(row["latency_cause"]): dict(row) for row in rows}


def _tail_row(
    scope: str,
    threshold: float,
    entries: Sequence[LatencyAttributionEntry],
) -> Dict[str, Any]:
    selected = [entry for entry in entries if entry.latency_us >= threshold]
    direct = sum(1 for entry in selected if entry.latency_cause == CAUSE_DIRECT_GC)
    spillover = sum(
        1 for entry in selected if entry.latency_cause == CAUSE_GC_SPILLOVER
    )
    workload = sum(
        1 for entry in selected if entry.latency_cause == CAUSE_WORKLOAD_QUEUE
    )
    unqueued = sum(1 for entry in selected if entry.latency_cause == CAUSE_UNQUEUED)
    gc_related = direct + spillover
    return {
        "tail_scope": scope,
        "latency_threshold_us": threshold,
        "request_count": len(selected),
        "direct_gc_requests": direct,
        "gc_spillover_requests": spillover,
        "workload_queue_requests": workload,
        "unqueued_requests": unqueued,
        "gc_related_requests": gc_related,
        "gc_related_ratio": gc_related / len(selected) if selected else 0.0,
    }


def build_tail_attribution_summary(
    entries: Sequence[LatencyAttributionEntry],
) -> List[Dict[str, Any]]:
    """Describe which causes populate p95, p99, p99.9, and max tails."""
    latencies = [entry.latency_us for entry in entries]
    if not latencies:
        thresholds = (("p95", 0.0), ("p99", 0.0), ("p99_9", 0.0), ("max", 0.0))
    else:
        thresholds = (
            ("p95", nearest_rank_percentile(latencies, 95.0)),
            ("p99", nearest_rank_percentile(latencies, 99.0)),
            ("p99_9", nearest_rank_percentile(latencies, 99.9)),
            ("max", max(latencies)),
        )
    return [_tail_row(scope, threshold, entries) for scope, threshold in thresholds]


def _prepare_output_path(output_dir: PathLike, filename: str) -> Path:
    if not filename or not filename.strip():
        raise ValueError("filename must not be empty")
    directory = Path(output_dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    return directory / filename


def save_latency_attribution_log(
    output_dir: PathLike,
    entries: Iterable[LatencyAttributionEntry],
    *,
    filename: str = "latency_attribution.csv",
) -> Path:
    path = _prepare_output_path(output_dir, filename)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ATTRIBUTION_FIELDS)
        writer.writeheader()
        for entry in entries:
            writer.writerow(entry.to_dict())
    return path


def _save_rows(
    output_dir: PathLike,
    rows: Sequence[Mapping[str, Any]],
    fields: Sequence[str],
    filename: str,
) -> Path:
    path = _prepare_output_path(output_dir, filename)
    expected = set(fields)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            if set(row.keys()) != expected:
                raise ValueError(
                    f"row schema mismatch for {filename}: expected={tuple(fields)}, "
                    f"got={tuple(row.keys())}"
                )
            writer.writerow(dict(row))
    return path


def save_cause_summary(
    output_dir: PathLike,
    rows: Sequence[Mapping[str, Any]],
    *,
    filename: str = "latency_cause_summary.csv",
) -> Path:
    return _save_rows(output_dir, rows, CAUSE_SUMMARY_FIELDS, filename)


def save_tail_attribution_summary(
    output_dir: PathLike,
    rows: Sequence[Mapping[str, Any]],
    *,
    filename: str = "tail_attribution_summary.csv",
) -> Path:
    return _save_rows(output_dir, rows, TAIL_SUMMARY_FIELDS, filename)


def attribution_sanity_checks(
    source_entries: Sequence[RequestLatencyEntry],
    attributed_entries: Sequence[LatencyAttributionEntry],
) -> Dict[str, bool]:
    causes = [entry.latency_cause for entry in attributed_entries]
    direct_count = sum(1 for cause in causes if cause == CAUSE_DIRECT_GC)
    gc_count = sum(1 for entry in source_entries if entry.gc_affected)
    fractions_valid = all(
        abs((entry.queue_fraction + entry.service_fraction) - 1.0) <= 1e-9
        if entry.latency_us > 0
        else entry.queue_fraction == 0.0 and entry.service_fraction == 0.0
        for entry in attributed_entries
    )
    queue_labels_valid = all(
        (entry.latency_cause in {CAUSE_DIRECT_GC, CAUSE_UNQUEUED})
        or entry.queue_delay_us > 0
        for entry in attributed_entries
    )
    spillover_has_cause = all(
        entry.causing_gc_request_id is not None
        for entry in attributed_entries
        if entry.latency_cause == CAUSE_GC_SPILLOVER
    )
    return {
        "attribution_count_identity": len(source_entries)
        == len(attributed_entries),
        "cause_partition_identity": len(causes)
        == sum(causes.count(cause) for cause in CAUSE_ORDER),
        "direct_gc_identity": direct_count == gc_count,
        "fraction_identity": fractions_valid,
        "queue_label_identity": queue_labels_valid,
        "spillover_has_gc_origin": spillover_has_cause,
    }
