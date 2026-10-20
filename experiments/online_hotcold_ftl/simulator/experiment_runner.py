from __future__ import annotations

import hashlib
import json
import platform
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Union

from .baseline_ftl import BaselineFTL
from .config import SSDConfig
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


PathLike = Union[str, Path]


@dataclass(frozen=True)
class PhasedBaselineRunResult:
    """Results and artifacts from one warm-up + measurement experiment."""

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



def _sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()



def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")



def _summary_sanity(
    summary: Dict[str, Any], total_pages: int
) -> Dict[str, bool]:
    return {
        "nand_write_identity": summary["nand_write_pages"]
        == summary["host_write_pages"] + summary["valid_page_migrations"],
        "gc_erase_identity": summary["gc_count"] == summary["erase_count"],
        "page_state_identity": (
            summary["free_pages"]
            + summary["valid_pages"]
            + summary["invalid_pages"]
            == total_pages
        ),
        "waf_not_below_one_when_writes_exist": (
            summary["host_write_pages"] == 0
            or summary["write_amplification_factor"] >= 1.0
        ),
    }



def _write_manifest(output_dir: Path, payload: Dict[str, Any]) -> Path:
    path = output_dir / "experiment_manifest.json"
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    return path



def run_phased_baseline_experiment(
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
    command_overhead_us: float = 20.0,
    read_page_us: float = 80.0,
    program_page_us: float = 200.0,
    erase_block_us: float = 2000.0,
) -> PhasedBaselineRunResult:
    """Run one reproducible warm-up + measured-latency experiment.

    The SSD is created once. Warm-up mutates the same mapping/NAND state later
    observed by measurement. Measurement requests are additionally passed through
    the deterministic Step 09 single-resource latency model. The resulting values
    are analytical costs for policy comparison, not hardware-calibrated timings.
    """
    if warmup_rounds < 0:
        raise ValueError(f"warmup_rounds must be >= 0: {warmup_rounds}")
    if measurement_rounds <= 0:
        raise ValueError(
            f"measurement_rounds must be > 0: {measurement_rounds}"
        )

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

    ftl = BaselineFTL(config, debug_check=debug_check)

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
        output,
        warmup_summary,
        filename="warmup_summary.csv",
    )
    summary_path = save_summary(
        output,
        measurement_summary,
        filename="baseline_summary.csv",
    )
    cumulative_summary_path = save_summary(
        output,
        cumulative_summary,
        filename="cumulative_summary.csv",
    )
    warmup_gc_log_path = save_gc_log(
        output,
        warmup_gc_log,
        filename="warmup_gc_log.csv",
    )
    gc_log_path = save_gc_log(
        output,
        measurement_gc_log,
        filename="gc_log.csv",
    )
    request_latency_path = save_request_latency_log(
        output,
        latency_recorder.entries,
        filename="request_latency.csv",
    )
    latency_summary_path = save_latency_summary(
        output,
        latency_rows,
        filename="latency_summary.csv",
    )
    latency_attribution_path = save_latency_attribution_log(
        output,
        attributed_entries,
        filename="latency_attribution.csv",
    )
    latency_cause_summary_path = save_cause_summary(
        output,
        cause_rows,
        filename="latency_cause_summary.csv",
    )
    tail_attribution_summary_path = save_tail_attribution_summary(
        output,
        tail_rows,
        filename="tail_attribution_summary.csv",
    )
    config_path = save_resolved_config(
        output,
        config,
        trace_stats=trace_stats,
        source_trace=trace_path,
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
        "schema_version": 3,
        "simulator": "baseline-ftl-v1+analytical-latency-v1+gc-attribution-v1",
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
                "event counters are phase deltas; page-state fields are "
                "end-of-phase gauges"
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
            "measurement": _summary_sanity(
                measurement_summary, config.total_pages
            ),
            "cumulative": _summary_sanity(
                cumulative_summary, config.total_pages
            ),
            "latency": latency_checks,
            "latency_attribution": attribution_checks,
        },
        "artifacts": artifact_names,
    }
    manifest_path = _write_manifest(output, manifest)

    return PhasedBaselineRunResult(
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
