from __future__ import annotations

import json
import platform
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Union

from .experiment_runner import _sha256_file, _summary_sanity, _utc_now_iso, _write_manifest
from .hot_cold_ftl import HotColdFTL
from .latency_attribution import (
    attribute_latency_causes,
    attribution_sanity_checks,
    build_cause_summary,
    build_tail_attribution_summary,
    cause_summary_by_name,
    save_cause_summary,
    save_latency_attribution_log,
    save_tail_attribution_summary,
)
from .latency_model import (
    LatencyModelConfig,
    RequestLatencyRecorder,
    build_latency_summary,
    latency_sanity_checks,
    latency_summary_by_scope,
    save_latency_summary,
    save_request_latency_log,
)
from .trace_io import (
    CAPACITY_BASIS_WRITE_WORKING_SET,
    TraceStats,
    resolve_ssd_config,
    save_gc_log,
    save_resolved_config,
    save_summary,
)
from .config import SSDConfig


PathLike = Union[str, Path]


@dataclass(frozen=True)
class PhasedHotColdRunResult:
    trace_stats: TraceStats
    config: SSDConfig
    warmup_summary: Dict[str, Any]
    summary: Dict[str, Any]
    cumulative_summary: Dict[str, Any]
    latency_summary: Dict[str, Dict[str, Any]]
    latency_cause_summary: Dict[str, Dict[str, Any]]
    tail_attribution_summary: Dict[str, Dict[str, Any]]
    warmup_summary_path: Path
    summary_path: Path
    cumulative_summary_path: Path
    warmup_gc_log_path: Path
    gc_log_path: Path
    request_latency_path: Path
    latency_summary_path: Path
    latency_attribution_path: Path
    latency_cause_summary_path: Path
    tail_attribution_summary_path: Path
    config_path: Path
    manifest_path: Path


def run_phased_hot_cold_experiment(
    input_path: PathLike,
    output_dir: PathLike,
    *,
    warmup_rounds: int = 0,
    measurement_rounds: int = 1,
    page_size: int = 4096,
    chips: int = 4,
    pages_per_block: int = 64,
    overprovision_ratio: float = 0.20,
    gc_threshold_ratio: float = 0.05,
    reserved_gc_blocks: int = 2,
    debug_check: bool = False,
    capacity_basis: str = CAPACITY_BASIS_WRITE_WORKING_SET,
    experiment_name: Optional[str] = None,
    hot_threshold: int = 2,
    hot_window_writes: int = 4096,
    command_overhead_us: float = 20.0,
    read_page_us: float = 80.0,
    program_page_us: float = 200.0,
    erase_block_us: float = 2000.0,
) -> PhasedHotColdRunResult:
    if warmup_rounds < 0:
        raise ValueError(f"warmup_rounds must be >= 0: {warmup_rounds}")
    if measurement_rounds <= 0:
        raise ValueError(f"measurement_rounds must be > 0: {measurement_rounds}")

    latency_model = LatencyModelConfig(
        command_overhead_us=command_overhead_us,
        read_page_us=read_page_us,
        program_page_us=program_page_us,
        erase_block_us=erase_block_us,
    )
    trace_path = Path(input_path).expanduser().resolve()
    output = Path(output_dir).expanduser()
    output.mkdir(parents=True, exist_ok=True)
    started_at = _utc_now_iso()

    trace_stats, config = resolve_ssd_config(
        trace_path,
        page_size=page_size,
        chips=chips,
        pages_per_block=pages_per_block,
        overprovision_ratio=overprovision_ratio,
        gc_threshold_ratio=gc_threshold_ratio,
        reserved_gc_blocks=reserved_gc_blocks,
        capacity_basis=capacity_basis,
    )
    ftl = HotColdFTL(
        config,
        debug_check=debug_check,
        hot_threshold=hot_threshold,
        hot_window_writes=hot_window_writes,
    )

    warmup_start = ftl.capture_counter_snapshot()
    ftl.run_trace_rounds(trace_path, warmup_rounds)
    warmup_summary = ftl.get_summary_since(warmup_start)
    warmup_gc_log = ftl.get_gc_log_since(warmup_start)

    measurement_start = ftl.capture_counter_snapshot()
    latency_recorder = RequestLatencyRecorder(latency_model)
    for round_index in range(1, measurement_rounds + 1):
        latency_recorder.run_trace_round(
            ftl,
            trace_path,
            round_index=round_index,
            trace_min_timestamp=trace_stats.min_timestamp,
        )
    measurement_summary = ftl.get_summary_since(measurement_start)
    measurement_gc_log = ftl.get_gc_log_since(measurement_start)
    ftl.assert_consistent()
    cumulative_summary = ftl.get_summary()

    latency_rows = build_latency_summary(latency_recorder.entries)
    latency_summary = latency_summary_by_scope(latency_rows)
    attributed_entries = attribute_latency_causes(latency_recorder.entries)
    cause_rows = build_cause_summary(attributed_entries)
    cause_summary = cause_summary_by_name(cause_rows)
    tail_rows = build_tail_attribution_summary(attributed_entries)
    tail_summary = {str(row["tail_scope"]): dict(row) for row in tail_rows}
    latency_checks = latency_sanity_checks(
        latency_recorder.entries,
        expected_requests=trace_stats.total_requests * measurement_rounds,
        measurement_summary=measurement_summary,
    )
    attribution_checks = attribution_sanity_checks(
        latency_recorder.entries, attributed_entries
    )

    warmup_summary_path = save_summary(
        output, warmup_summary, filename="warmup_summary.csv"
    )
    summary_path = save_summary(
        output, measurement_summary, filename="hot_cold_summary.csv"
    )
    cumulative_summary_path = save_summary(
        output, cumulative_summary, filename="cumulative_summary.csv"
    )
    warmup_gc_log_path = save_gc_log(
        output, warmup_gc_log, filename="warmup_gc_log.csv"
    )
    gc_log_path = save_gc_log(output, measurement_gc_log, filename="gc_log.csv")
    request_latency_path = save_request_latency_log(
        output, latency_recorder.entries, filename="request_latency.csv"
    )
    latency_summary_path = save_latency_summary(
        output, latency_rows, filename="latency_summary.csv"
    )
    latency_attribution_path = save_latency_attribution_log(
        output, attributed_entries, filename="latency_attribution.csv"
    )
    latency_cause_summary_path = save_cause_summary(
        output, cause_rows, filename="latency_cause_summary.csv"
    )
    tail_attribution_summary_path = save_tail_attribution_summary(
        output, tail_rows, filename="tail_attribution_summary.csv"
    )
    config_path = save_resolved_config(
        output, config, trace_stats=trace_stats, source_trace=trace_path
    )

    finished_at = _utc_now_iso()
    artifact_names = {
        "warmup_summary": warmup_summary_path.name,
        "measurement_summary": summary_path.name,
        "cumulative_summary": cumulative_summary_path.name,
        "warmup_gc_log": warmup_gc_log_path.name,
        "measurement_gc_log": gc_log_path.name,
        "request_latency": request_latency_path.name,
        "latency_summary": latency_summary_path.name,
        "latency_attribution": latency_attribution_path.name,
        "latency_cause_summary": latency_cause_summary_path.name,
        "tail_attribution_summary": tail_attribution_summary_path.name,
        "resolved_config": config_path.name,
        "manifest": "experiment_manifest.json",
    }
    manifest = {
        "schema_version": 4,
        "simulator": "online-hot-cold-ftl-v1+analytical-latency-v1+gc-attribution-v1",
        "experiment_name": experiment_name or output.name,
        "started_at_utc": started_at,
        "finished_at_utc": finished_at,
        "runtime": {
            "python_version": sys.version.split()[0],
            "platform": platform.platform(),
        },
        "input_trace": {
            "path": str(trace_path),
            "size_bytes": trace_path.stat().st_size,
            "sha256": _sha256_file(trace_path),
        },
        "phase_plan": {
            "warmup_rounds": warmup_rounds,
            "measurement_rounds": measurement_rounds,
            "state_reset_between_phases": False,
            "measurement_queue_reset_after_warmup": True,
            "counter_semantics": (
                "event counters are phase deltas; page-state and policy-state "
                "fields are end-of-phase gauges"
            ),
        },
        "parameters": {
            "page_size": page_size,
            "chips": chips,
            "pages_per_block": pages_per_block,
            "overprovision_ratio": overprovision_ratio,
            "gc_threshold_ratio": gc_threshold_ratio,
            "reserved_gc_blocks": reserved_gc_blocks,
            "capacity_basis": capacity_basis,
            "debug_check": debug_check,
            "hot_threshold": hot_threshold,
            "hot_window_writes": hot_window_writes,
            "future_information_used": False,
            "fixed_hot_cold_partition": False,
            "shared_free_block_pool": True,
        },
        "latency_model": latency_model.to_dict(),
        "trace_stats": trace_stats.to_dict(),
        "ssd_config": config.to_dict(),
        "phase_results": {
            "warmup": warmup_summary,
            "measurement": measurement_summary,
            "cumulative": cumulative_summary,
        },
        "latency_results": latency_summary,
        "latency_cause_results": cause_summary,
        "tail_attribution_results": tail_summary,
        "sanity_checks": {
            "warmup": _summary_sanity(warmup_summary, config.total_pages),
            "measurement": _summary_sanity(measurement_summary, config.total_pages),
            "cumulative": _summary_sanity(cumulative_summary, config.total_pages),
            "latency": latency_checks,
            "latency_attribution": attribution_checks,
            "placement_accounting": {
                "host_write_temperature_identity": (
                    measurement_summary["hot_host_write_pages"]
                    + measurement_summary["cold_host_write_pages"]
                    == measurement_summary["host_write_pages"]
                ),
                "migration_temperature_identity": (
                    measurement_summary["hot_gc_migration_pages"]
                    + measurement_summary["cold_gc_migration_pages"]
                    == measurement_summary["valid_page_migrations"]
                ),
            },
        },
        "artifacts": artifact_names,
    }
    manifest_path = _write_manifest(output, manifest)

    return PhasedHotColdRunResult(
        trace_stats=trace_stats,
        config=config,
        warmup_summary=warmup_summary,
        summary=measurement_summary,
        cumulative_summary=cumulative_summary,
        latency_summary=latency_summary,
        latency_cause_summary=cause_summary,
        tail_attribution_summary=tail_summary,
        warmup_summary_path=warmup_summary_path,
        summary_path=summary_path,
        cumulative_summary_path=cumulative_summary_path,
        warmup_gc_log_path=warmup_gc_log_path,
        gc_log_path=gc_log_path,
        request_latency_path=request_latency_path,
        latency_summary_path=latency_summary_path,
        latency_attribution_path=latency_attribution_path,
        latency_cause_summary_path=latency_cause_summary_path,
        tail_attribution_summary_path=tail_attribution_summary_path,
        config_path=config_path,
        manifest_path=manifest_path,
    )
