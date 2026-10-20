from __future__ import annotations

import csv
import json
import math
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from .latency_model import nearest_rank_percentile
from .trace_io import iter_standard_trace


PathLike = Union[str, Path]
TraceRow = Mapping[str, Any]


@dataclass(frozen=True)
class WorkloadProfileConfig:
    window_seconds: float = 1.0
    burst_percentile: float = 95.0
    top_hot_lpn_count: int = 100
    max_tracked_write_pages: int = 5_000_000

    def __post_init__(self) -> None:
        if not math.isfinite(self.window_seconds) or self.window_seconds <= 0:
            raise ValueError("window_seconds must be finite and > 0")
        if not 0 < self.burst_percentile <= 100:
            raise ValueError("burst_percentile must be in (0, 100]")
        if self.top_hot_lpn_count < 0:
            raise ValueError("top_hot_lpn_count must be >= 0")
        if self.max_tracked_write_pages <= 0:
            raise ValueError("max_tracked_write_pages must be > 0")


@dataclass(frozen=True)
class WindowStat:
    window_index: int
    start_time_s: float
    end_time_s: float
    request_count: int
    read_requests: int
    write_requests: int
    read_pages: int
    write_pages: int
    request_rate_iops: float
    page_rate_pages_per_s: float
    write_request_ratio: float
    write_page_ratio: float
    is_burst: bool

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HotLPNStat:
    rank: int
    lpn: int
    write_count: int

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WorkloadProfileResult:
    summary: Dict[str, Any]
    windows: Tuple[WindowStat, ...]
    hot_lpns: Tuple[HotLPNStat, ...]
    summary_csv_path: Path
    summary_json_path: Path
    window_stats_path: Path
    burst_intervals_path: Path
    hot_lpn_path: Path
    manifest_path: Path


def _safe_ratio(numerator: float, denominator: float) -> float:
    return float(numerator) / float(denominator) if denominator else 0.0


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _write_csv(path: Path, fieldnames: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(dict(row))


def _window_index(timestamp: float, origin: float, window_seconds: float) -> int:
    # Small epsilon avoids classifying an exact boundary below its intended bucket
    # due to floating-point representation.
    relative = max(0.0, timestamp - origin)
    return int(math.floor((relative + 1e-12) / window_seconds))


def profile_standard_trace(
    input_path: PathLike,
    output_dir: PathLike,
    *,
    window_seconds: float = 1.0,
    burst_percentile: float = 95.0,
    top_hot_lpn_count: int = 100,
    max_tracked_write_pages: int = 5_000_000,
) -> WorkloadProfileResult:
    """Profile a bounded standard trace for workload-aware FTL design.

    The analyzer is intentionally dataset-independent. It consumes only the
    simulator's canonical page-based trace contract and therefore can be reused
    unchanged for MSR, Alibaba 2020, Alibaba EBS 2026, or future adapters.

    Burst detection uses the nearest-rank percentile of request counts among
    non-empty fixed-width windows. This is a trace-local descriptive threshold,
    not a universal production SLO.
    """

    config = WorkloadProfileConfig(
        window_seconds=window_seconds,
        burst_percentile=burst_percentile,
        top_hot_lpn_count=top_hot_lpn_count,
        max_tracked_write_pages=max_tracked_write_pages,
    )
    output = Path(output_dir).expanduser()
    output.mkdir(parents=True, exist_ok=True)

    rows = iter_standard_trace(input_path)

    total_requests = 0
    read_requests = 0
    write_requests = 0
    read_pages = 0
    write_pages = 0
    request_sizes: List[float] = []
    interarrival_us: List[float] = []
    min_timestamp: Optional[float] = None
    max_timestamp: Optional[float] = None
    previous_timestamp: Optional[float] = None

    sequential_transitions = 0
    comparable_transitions = 0
    previous_by_flow: Dict[str, Tuple[str, int, int]] = {}

    window_accumulator: Dict[int, Dict[str, int]] = defaultdict(
        lambda: {
            "request_count": 0,
            "read_requests": 0,
            "write_requests": 0,
            "read_pages": 0,
            "write_pages": 0,
        }
    )

    written_pages: set[int] = set()
    write_frequency: Counter[int] = Counter()
    repeated_write_pages = 0
    overwrite_requests = 0
    hotness_tracking_complete = True

    cached_rows: List[TraceRow] = []
    # We need the first timestamp as a window origin. Store rows only until it is
    # known, then process streaming. In practice this list contains one row.
    origin: Optional[float] = None

    def process_row(row: TraceRow) -> None:
        nonlocal total_requests, read_requests, write_requests
        nonlocal read_pages, write_pages, min_timestamp, max_timestamp
        nonlocal previous_timestamp, sequential_transitions, comparable_transitions
        nonlocal repeated_write_pages, overwrite_requests, hotness_tracking_complete

        timestamp = float(row["timestamp"])
        op = str(row["op"])
        lba = int(row["lba"])
        size = int(row["size"])
        flow_id = str(row["flow_id"])

        total_requests += 1
        request_sizes.append(float(size))
        min_timestamp = timestamp if min_timestamp is None else min(min_timestamp, timestamp)
        max_timestamp = timestamp if max_timestamp is None else max(max_timestamp, timestamp)

        if previous_timestamp is not None:
            interarrival_us.append(max(0.0, timestamp - previous_timestamp) * 1_000_000.0)
        previous_timestamp = timestamp

        previous = previous_by_flow.get(flow_id)
        if previous is not None:
            previous_op, previous_lba, previous_size = previous
            comparable_transitions += 1
            if previous_op == op and lba == previous_lba + previous_size:
                sequential_transitions += 1
        previous_by_flow[flow_id] = (op, lba, size)

        assert origin is not None
        index = _window_index(timestamp, origin, config.window_seconds)
        bucket = window_accumulator[index]
        bucket["request_count"] += 1

        if op == "R":
            read_requests += 1
            read_pages += size
            bucket["read_requests"] += 1
            bucket["read_pages"] += size
        else:
            write_requests += 1
            write_pages += size
            bucket["write_requests"] += 1
            bucket["write_pages"] += size

            if hotness_tracking_complete:
                if len(written_pages) + size > config.max_tracked_write_pages:
                    hotness_tracking_complete = False
                    written_pages.clear()
                    write_frequency.clear()
                else:
                    request_overwrote = False
                    for page in range(lba, lba + size):
                        if page in written_pages:
                            repeated_write_pages += 1
                            request_overwrote = True
                        else:
                            written_pages.add(page)
                        write_frequency[page] += 1
                    if request_overwrote:
                        overwrite_requests += 1

    for row in rows:
        if origin is None:
            origin = float(row["timestamp"])
        process_row(row)

    if total_requests == 0 or origin is None or min_timestamp is None or max_timestamp is None:
        raise ValueError("standard trace contains no requests")

    window_request_counts = [
        float(values["request_count"])
        for _, values in sorted(window_accumulator.items())
    ]
    burst_threshold = nearest_rank_percentile(
        window_request_counts,
        config.burst_percentile,
    )

    windows: List[WindowStat] = []
    burst_rows: List[Dict[str, Any]] = []
    for index, values in sorted(window_accumulator.items()):
        start = origin + index * config.window_seconds
        end = start + config.window_seconds
        request_count = values["request_count"]
        total_pages = values["read_pages"] + values["write_pages"]
        is_burst = request_count >= burst_threshold and request_count > 0
        item = WindowStat(
            window_index=index,
            start_time_s=start,
            end_time_s=end,
            request_count=request_count,
            read_requests=values["read_requests"],
            write_requests=values["write_requests"],
            read_pages=values["read_pages"],
            write_pages=values["write_pages"],
            request_rate_iops=request_count / config.window_seconds,
            page_rate_pages_per_s=total_pages / config.window_seconds,
            write_request_ratio=_safe_ratio(values["write_requests"], request_count),
            write_page_ratio=_safe_ratio(values["write_pages"], total_pages),
            is_burst=is_burst,
        )
        windows.append(item)
        if is_burst:
            burst_rows.append(item.to_dict())

    hot_lpns: List[HotLPNStat] = []
    if hotness_tracking_complete and config.top_hot_lpn_count > 0:
        ordered_hot = sorted(write_frequency.items(), key=lambda pair: (-pair[1], pair[0]))
        for rank, (lpn, count) in enumerate(
            ordered_hot[: config.top_hot_lpn_count], start=1
        ):
            hot_lpns.append(HotLPNStat(rank=rank, lpn=lpn, write_count=count))

    duration = max_timestamp - min_timestamp
    total_pages = read_pages + write_pages
    nonempty_window_count = len(windows)
    burst_window_count = sum(1 for window in windows if window.is_burst)

    summary: Dict[str, Any] = {
        "input_path": str(Path(input_path).expanduser()),
        "total_requests": total_requests,
        "read_requests": read_requests,
        "write_requests": write_requests,
        "read_pages": read_pages,
        "write_pages": write_pages,
        "total_pages": total_pages,
        "duration_s": duration,
        "read_request_ratio": _safe_ratio(read_requests, total_requests),
        "write_request_ratio": _safe_ratio(write_requests, total_requests),
        "write_page_ratio": _safe_ratio(write_pages, total_pages),
        "avg_request_size_pages": _mean(request_sizes),
        "p50_request_size_pages": nearest_rank_percentile(request_sizes, 50),
        "p95_request_size_pages": nearest_rank_percentile(request_sizes, 95),
        "p99_request_size_pages": nearest_rank_percentile(request_sizes, 99),
        "avg_interarrival_us": _mean(interarrival_us),
        "p50_interarrival_us": nearest_rank_percentile(interarrival_us, 50),
        "p95_interarrival_us": nearest_rank_percentile(interarrival_us, 95),
        "p99_interarrival_us": nearest_rank_percentile(interarrival_us, 99),
        "sequential_transition_ratio": _safe_ratio(
            sequential_transitions, comparable_transitions
        ),
        "sequential_transitions": sequential_transitions,
        "comparable_transitions": comparable_transitions,
        "window_seconds": config.window_seconds,
        "nonempty_window_count": nonempty_window_count,
        "burst_percentile": config.burst_percentile,
        "burst_request_threshold_per_window": burst_threshold,
        "burst_iops_threshold": burst_threshold / config.window_seconds,
        "burst_window_count": burst_window_count,
        "burst_window_ratio": _safe_ratio(burst_window_count, nonempty_window_count),
        "max_window_request_count": max(window_request_counts) if window_request_counts else 0.0,
        "max_window_iops": (
            max(window_request_counts) / config.window_seconds
            if window_request_counts
            else 0.0
        ),
        "hotness_tracking_complete": hotness_tracking_complete,
        "max_tracked_write_pages": config.max_tracked_write_pages,
        "distinct_written_pages": len(written_pages) if hotness_tracking_complete else -1,
        "repeated_write_pages": repeated_write_pages if hotness_tracking_complete else -1,
        "overwrite_page_ratio": (
            _safe_ratio(repeated_write_pages, write_pages)
            if hotness_tracking_complete
            else -1.0
        ),
        "overwrite_requests": overwrite_requests if hotness_tracking_complete else -1,
        "overwrite_request_ratio": (
            _safe_ratio(overwrite_requests, write_requests)
            if hotness_tracking_complete
            else -1.0
        ),
    }

    summary_csv_path = output / "workload_summary.csv"
    summary_json_path = output / "workload_summary.json"
    window_stats_path = output / "window_stats.csv"
    burst_intervals_path = output / "burst_intervals.csv"
    hot_lpn_path = output / "hot_lpn.csv"
    manifest_path = output / "workload_profile_manifest.json"

    _write_csv(summary_csv_path, ("metric", "value"), (
        {"metric": key, "value": value} for key, value in summary.items()
    ))
    with summary_json_path.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, sort_keys=True)
        handle.write("\n")

    window_fields = tuple(WindowStat.__dataclass_fields__.keys())
    _write_csv(window_stats_path, window_fields, (window.to_dict() for window in windows))
    _write_csv(burst_intervals_path, window_fields, burst_rows)
    hot_fields = tuple(HotLPNStat.__dataclass_fields__.keys())
    _write_csv(hot_lpn_path, hot_fields, (item.to_dict() for item in hot_lpns))

    manifest = {
        "stage": "step14-dataset-independent-workload-profile",
        "config": asdict(config),
        "artifacts": {
            "summary_csv": str(summary_csv_path),
            "summary_json": str(summary_json_path),
            "window_stats": str(window_stats_path),
            "burst_intervals": str(burst_intervals_path),
            "hot_lpn": str(hot_lpn_path),
        },
        "method_notes": {
            "burst_threshold": "nearest-rank percentile of request counts among non-empty fixed windows",
            "sequentiality": "same-flow consecutive requests with same op and next_lba == previous_end_lba",
            "overwrite": "exact page-level tracking only while tracked page budget is not exceeded",
        },
    }
    with manifest_path.open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")

    return WorkloadProfileResult(
        summary=summary,
        windows=tuple(windows),
        hot_lpns=tuple(hot_lpns),
        summary_csv_path=summary_csv_path,
        summary_json_path=summary_json_path,
        window_stats_path=window_stats_path,
        burst_intervals_path=burst_intervals_path,
        hot_lpn_path=hot_lpn_path,
        manifest_path=manifest_path,
    )
