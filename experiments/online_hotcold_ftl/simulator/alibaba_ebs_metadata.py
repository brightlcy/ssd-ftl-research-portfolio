from __future__ import annotations

import csv
import json
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union


PathLike = Union[str, Path]
METADATA_COLUMNS = ("volume_id", "storage_cluster_name", "ecstag_list", "size(MB)")
PILOT_COLUMNS = (
    "workload_type",
    "rank_within_workload",
    "volume_id",
    "cluster_id",
    "trace_size_mb",
    "expected_relative_path",
    "selection_purpose",
)


@dataclass(frozen=True)
class AlibabaEBSMetadataRow:
    volume_id: int
    cluster_id: int
    workload_type: str
    trace_size_mb: float

    @property
    def expected_relative_path(self) -> str:
        return (
            f"{self.workload_type}/"
            f"cluster{self.cluster_id}-dev{self.volume_id}.csv"
        )

    def to_dict(self) -> Dict[str, object]:
        data = asdict(self)
        data["expected_relative_path"] = self.expected_relative_path
        return data


@dataclass(frozen=True)
class AlibabaEBSPilotCandidate:
    workload_type: str
    rank_within_workload: int
    volume_id: int
    cluster_id: int
    trace_size_mb: float
    expected_relative_path: str
    selection_purpose: str = "acquisition_pilot_not_final_representative"

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class AlibabaEBSPilotPlan:
    metadata_path: Path
    output_dir: Path
    candidate_path: Path
    summary_path: Path
    manifest_path: Path
    row_count: int
    candidates: Tuple[AlibabaEBSPilotCandidate, ...]


def _parse_int(value: str, *, column: str, row_number: int, path: Path) -> int:
    try:
        parsed = int(value.strip())
    except ValueError as exc:
        raise ValueError(
            f"row {row_number}: {column} must be an integer, got {value!r}: {path}"
        ) from exc
    if parsed < 0:
        raise ValueError(f"row {row_number}: {column} must be >= 0: {path}")
    return parsed


def _parse_float(value: str, *, column: str, row_number: int, path: Path) -> float:
    try:
        parsed = float(value.strip())
    except ValueError as exc:
        raise ValueError(
            f"row {row_number}: {column} must be numeric, got {value!r}: {path}"
        ) from exc
    if parsed <= 0:
        raise ValueError(f"row {row_number}: {column} must be > 0: {path}")
    return parsed


def load_alibaba_ebs_metadata(path_like: PathLike) -> Tuple[AlibabaEBSMetadataRow, ...]:
    path = Path(path_like).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"Alibaba EBS metadata does not exist: {path}")
    rows: List[AlibabaEBSMetadataRow] = []
    seen: set[Tuple[int, int, str]] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = [name.strip().lstrip("\ufeff") for name in (reader.fieldnames or [])]
        if not all(column in fields for column in METADATA_COLUMNS):
            raise ValueError(
                f"metadata requires columns {METADATA_COLUMNS}: found={fields}: {path}"
            )
        for row_number, row in enumerate(reader, start=2):
            if not row or all(not str(value or "").strip() for value in row.values()):
                continue
            workload = str(row["ecstag_list"] or "").strip().lower()
            if not workload:
                raise ValueError(f"row {row_number}: ecstag_list must not be empty: {path}")
            item = AlibabaEBSMetadataRow(
                volume_id=_parse_int(
                    str(row["volume_id"]),
                    column="volume_id",
                    row_number=row_number,
                    path=path,
                ),
                cluster_id=_parse_int(
                    str(row["storage_cluster_name"]),
                    column="storage_cluster_name",
                    row_number=row_number,
                    path=path,
                ),
                workload_type=workload,
                trace_size_mb=_parse_float(
                    str(row["size(MB)"]),
                    column="size(MB)",
                    row_number=row_number,
                    path=path,
                ),
            )
            key = (item.cluster_id, item.volume_id, item.workload_type)
            if key in seen:
                raise ValueError(f"row {row_number}: duplicate metadata key {key}: {path}")
            seen.add(key)
            rows.append(item)
    if not rows:
        raise ValueError(f"metadata is empty: {path}")
    return tuple(rows)


def select_pilot_candidates(
    rows: Sequence[AlibabaEBSMetadataRow],
    *,
    workloads: Sequence[str] = ("mysql", "mq", "k8s"),
    per_workload: int = 2,
    min_size_mb: float = 64.0,
    max_size_mb: float = 160.0,
    target_size_mb: float = 100.0,
) -> Tuple[AlibabaEBSPilotCandidate, ...]:
    """Select download-friendly pilot files, not final representative workloads.

    File size is used only to control acquisition cost. Cluster diversity is preferred
    so that the pilot is not accidentally drawn from one storage cluster. Final trace
    selection must be based on request-level I/O statistics after files are inspected.
    """
    if per_workload <= 0:
        raise ValueError("per_workload must be > 0")
    if min_size_mb <= 0 or max_size_mb < min_size_mb:
        raise ValueError("invalid size range")
    if target_size_mb < min_size_mb or target_size_mb > max_size_mb:
        raise ValueError("target_size_mb must fall inside the size range")

    result: List[AlibabaEBSPilotCandidate] = []
    for raw_workload in workloads:
        workload = raw_workload.strip().lower()
        eligible = [
            item
            for item in rows
            if item.workload_type == workload
            and min_size_mb <= item.trace_size_mb <= max_size_mb
        ]
        if len(eligible) < per_workload:
            raise ValueError(
                f"not enough {workload} traces inside {min_size_mb}-{max_size_mb}MB: "
                f"found={len(eligible)}, required={per_workload}"
            )
        ordered = sorted(
            eligible,
            key=lambda item: (
                abs(item.trace_size_mb - target_size_mb),
                item.trace_size_mb,
                item.cluster_id,
                item.volume_id,
            ),
        )
        selected: List[AlibabaEBSMetadataRow] = []
        used_clusters: set[int] = set()
        for item in ordered:
            if item.cluster_id in used_clusters:
                continue
            selected.append(item)
            used_clusters.add(item.cluster_id)
            if len(selected) == per_workload:
                break
        if len(selected) < per_workload:
            used_keys = {(item.cluster_id, item.volume_id) for item in selected}
            for item in ordered:
                if (item.cluster_id, item.volume_id) in used_keys:
                    continue
                selected.append(item)
                if len(selected) == per_workload:
                    break

        for rank, item in enumerate(selected, start=1):
            result.append(
                AlibabaEBSPilotCandidate(
                    workload_type=workload,
                    rank_within_workload=rank,
                    volume_id=item.volume_id,
                    cluster_id=item.cluster_id,
                    trace_size_mb=item.trace_size_mb,
                    expected_relative_path=item.expected_relative_path,
                )
            )
    return tuple(result)


def _percentile(sorted_values: Sequence[float], fraction: float) -> float:
    if not sorted_values:
        return 0.0
    index = max(0, min(len(sorted_values) - 1, int(round((len(sorted_values) - 1) * fraction))))
    return float(sorted_values[index])


def write_pilot_plan(
    metadata_path: PathLike,
    output_dir: PathLike,
    *,
    workloads: Sequence[str] = ("mysql", "mq", "k8s"),
    per_workload: int = 2,
    min_size_mb: float = 64.0,
    max_size_mb: float = 160.0,
    target_size_mb: float = 100.0,
) -> AlibabaEBSPilotPlan:
    source = Path(metadata_path).expanduser()
    rows = load_alibaba_ebs_metadata(source)
    candidates = select_pilot_candidates(
        rows,
        workloads=workloads,
        per_workload=per_workload,
        min_size_mb=min_size_mb,
        max_size_mb=max_size_mb,
        target_size_mb=target_size_mb,
    )
    out = Path(output_dir).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    candidate_path = out / "pilot_candidates.csv"
    summary_path = out / "metadata_workload_summary.csv"
    manifest_path = out / "pilot_selection_manifest.json"

    with candidate_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PILOT_COLUMNS)
        writer.writeheader()
        for item in candidates:
            writer.writerow(item.to_dict())

    grouped: Dict[str, List[AlibabaEBSMetadataRow]] = defaultdict(list)
    for item in rows:
        grouped[item.workload_type].append(item)
    summary_columns = (
        "workload_type",
        "volume_count",
        "cluster_count",
        "min_size_mb",
        "p25_size_mb",
        "median_size_mb",
        "p75_size_mb",
        "max_size_mb",
        "total_size_gb",
    )
    with summary_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=summary_columns)
        writer.writeheader()
        for workload in sorted(grouped):
            items = grouped[workload]
            sizes = sorted(item.trace_size_mb for item in items)
            writer.writerow(
                {
                    "workload_type": workload,
                    "volume_count": len(items),
                    "cluster_count": len({item.cluster_id for item in items}),
                    "min_size_mb": min(sizes),
                    "p25_size_mb": _percentile(sizes, 0.25),
                    "median_size_mb": statistics.median(sizes),
                    "p75_size_mb": _percentile(sizes, 0.75),
                    "max_size_mb": max(sizes),
                    "total_size_gb": sum(sizes) / 1024.0,
                }
            )

    payload = {
        "schema_version": 1,
        "source_metadata": str(source.resolve()),
        "metadata_row_count": len(rows),
        "workload_counts": dict(sorted(Counter(item.workload_type for item in rows).items())),
        "selection_parameters": {
            "workloads": [item.strip().lower() for item in workloads],
            "per_workload": per_workload,
            "min_size_mb": min_size_mb,
            "max_size_mb": max_size_mb,
            "target_size_mb": target_size_mb,
            "prefer_cluster_diversity": True,
        },
        "methodological_guardrail": (
            "Candidates are selected only for download feasibility and schema/pipeline "
            "validation. They must not be called representative until request-level "
            "read/write ratio, request size, locality, overwrite, and burstiness are analyzed."
        ),
        "candidates": [item.to_dict() for item in candidates],
    }
    with manifest_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")

    return AlibabaEBSPilotPlan(
        metadata_path=source,
        output_dir=out,
        candidate_path=candidate_path,
        summary_path=summary_path,
        manifest_path=manifest_path,
        row_count=len(rows),
        candidates=candidates,
    )
