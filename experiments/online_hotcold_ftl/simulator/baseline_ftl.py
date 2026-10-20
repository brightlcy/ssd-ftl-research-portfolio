from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Union

from .config import SSDConfig
from .gc_policy import GreedyGCPolicy, NoVictimBlockError, VictimCandidate
from .metadata import FTLMetadata, NoFreePageError
from .trace_io import iter_standard_trace


PathLike = Union[str, Path]
TraceRow = Mapping[str, Any]


class BaselineFTLError(RuntimeError):
    """Base exception for failures in the baseline FTL execution path."""


class InsufficientGCWorkspaceError(BaselineFTLError):
    """Raised when a selected victim cannot be migrated with current free pages."""


@dataclass(frozen=True)
class FTLCounterSnapshot:
    """Cumulative event counters captured at one experiment boundary.

    Metadata state is intentionally not copied. A phase summary subtracts these
    counters while reporting the live metadata state at the end of the phase.
    This allows warm-up and measurement to share one continuously evolving SSD.
    """

    host_write_requests: int
    host_write_pages: int
    host_read_requests: int
    host_read_pages: int
    nand_write_pages: int
    valid_page_migrations: int
    gc_count: int
    erase_count: int
    gc_log_length: int


@dataclass(frozen=True)
class GCLogEntry:
    """Immutable accounting record for one completed garbage-collection cycle."""

    gc_id: int
    victim_block: int
    valid_pages_before: int
    invalid_pages_before: int
    free_pages_before: int
    migrated_pages: int
    free_pages_after: int
    victim_erase_count: int

    @property
    def net_reclaimed_pages(self) -> int:
        """Net free-page gain produced by this GC cycle."""
        return self.free_pages_after - self.free_pages_before

    def to_dict(self) -> Dict[str, int]:
        data = asdict(self)
        data["net_reclaimed_pages"] = self.net_reclaimed_pages
        return data


class BaselineFTL:
    """Deterministic page-mapping FTL with greedy garbage collection.

    Baseline v1 models the functional data path and write amplification:

      * Host writes are split into page writes.
      * An overwrite invalidates the previous physical page.
      * New host data is written out-of-place to a free physical page.
      * At the low-free-page threshold, one eligible closed block is collected.
      * Valid pages in the victim are copied before the victim is erased.
      * Reads are counted but do not consume NAND pages in this functional model.

    The model intentionally excludes timing, channels, queues, cache effects,
    wear-leveling policy, and NAND read/program/erase latency. Those belong to
    later versions after the functional accounting is proven correct.
    """

    def __init__(
        self,
        config: SSDConfig,
        *,
        metadata: Optional[FTLMetadata] = None,
        gc_policy: Optional[GreedyGCPolicy] = None,
        debug_check: bool = False,
    ) -> None:
        self.config = config
        self.metadata = metadata if metadata is not None else FTLMetadata(config)
        if self.metadata.config != config:
            raise ValueError("metadata config must match BaselineFTL config")

        self.gc_policy = gc_policy if gc_policy is not None else GreedyGCPolicy()
        self.debug_check = debug_check

        self.host_write_requests = 0
        self.host_write_pages = 0
        self.host_read_requests = 0
        self.host_read_pages = 0
        self.nand_write_pages = 0
        self.valid_page_migrations = 0
        self.gc_count = 0
        self.gc_log: List[GCLogEntry] = []

    def _validate_lpn(self, lpn: int) -> None:
        if lpn < 0:
            raise ValueError(f"LPN must be >= 0: {lpn}")

    def _validate_request(self, lba: int, size: int) -> None:
        if lba < 0:
            raise ValueError(f"lba must be >= 0: {lba}")
        if size <= 0:
            raise ValueError(f"size must be > 0 pages: {size}")
        self._validate_lpn(lba)
        self._validate_lpn(lba + size - 1)

    def _validate_new_mapping_capacity(self, lpns: range) -> None:
        """Ensure a request cannot create more live mappings than capacity.

        External trace LPNs may be sparse and numerically much larger than the
        configured logical capacity. Capacity therefore constrains the number of
        simultaneously mapped LPNs, not the maximum numeric LPN identifier.
        """
        if self.config.logical_pages is None:
            return

        new_mappings = sum(
            1 for lpn in lpns if lpn not in self.metadata.mapping_table
        )
        projected = len(self.metadata.mapping_table) + new_mappings
        if projected > self.config.logical_pages:
            raise ValueError(
                "write would exceed configured logical mapping capacity: "
                f"projected={projected}, capacity={self.config.logical_pages}"
            )

    def should_run_gc(self) -> bool:
        """Return whether the free pool reached the configured low watermark."""
        return self.metadata.free_page_count <= self.config.gc_threshold_pages

    def _select_victim(self) -> VictimCandidate:
        candidate = self.gc_policy.select_victim(self.metadata)
        if candidate.valid_pages > self.metadata.free_page_count:
            raise InsufficientGCWorkspaceError(
                "not enough free pages to migrate the selected victim: "
                f"victim={candidate.block_id}, valid={candidate.valid_pages}, "
                f"free={self.metadata.free_page_count}"
            )
        return candidate

    def run_gc(self) -> GCLogEntry:
        """Run one complete valid-page migration and block erase cycle."""
        candidate = self._select_victim()
        victim_block = candidate.block_id
        valid_pages = self.metadata.get_valid_pages_in_block(victim_block)

        # Snapshot and reserve all destinations first. The workspace check above
        # guarantees that allocation succeeds without recursively invoking GC.
        destination_ppns: List[int] = []
        try:
            for _ in valid_pages:
                destination_ppns.append(self.metadata.allocate_free_page())
        except NoFreePageError as exc:  # Defensive: should be prevented above.
            for ppn in destination_ppns:
                self.metadata.release_reserved_page(ppn)
            raise InsufficientGCWorkspaceError(
                "free-page pool changed while reserving GC destinations"
            ) from exc

        free_pages_before = self.metadata.free_page_count + len(destination_ppns)

        migrated_pages = 0
        for (old_ppn, lpn), new_ppn in zip(valid_pages, destination_ppns):
            current_ppn = self.metadata.mapping_table.get(lpn)
            if current_ppn != old_ppn:
                raise BaselineFTLError(
                    "mapping changed during GC migration: "
                    f"LPN {lpn}, expected PPN {old_ppn}, found {current_ppn}"
                )

            self.metadata.invalidate_old_page(old_ppn)
            self.metadata.register_valid_page(lpn, new_ppn)
            migrated_pages += 1
            self.valid_page_migrations += 1
            self.nand_write_pages += 1

        self.metadata.erase_block(victim_block)
        self.gc_count += 1

        entry = GCLogEntry(
            gc_id=self.gc_count,
            victim_block=victim_block,
            valid_pages_before=candidate.valid_pages,
            invalid_pages_before=candidate.invalid_pages,
            free_pages_before=free_pages_before,
            migrated_pages=migrated_pages,
            free_pages_after=self.metadata.free_page_count,
            victim_erase_count=self.metadata.get_block_info(victim_block).erase_count,
        )
        self.gc_log.append(entry)

        if self.debug_check:
            self.assert_consistent()
        return entry

    def _maybe_run_gc(self) -> Optional[GCLogEntry]:
        """Run one GC cycle at the threshold when a useful victim exists.

        Reaching the threshold alone is not an error. During initial filling,
        every programmed page may still be valid, so there can be no useful
        victim yet. In that case the host write may consume another free page;
        a later overwrite will create reclaimable invalid data.
        """
        if not self.should_run_gc():
            return None
        try:
            return self.run_gc()
        except NoVictimBlockError:
            return None

    def write_lpn(self, lpn: int) -> int:
        """Process one host page write and return the new physical page number."""
        self._validate_lpn(lpn)

        # Never destroy the current mapping if the free pool is already empty.
        # Existing stale pages must be reclaimed first.
        if self.metadata.free_page_count == 0:
            self.run_gc()

        old_ppn = self.metadata.mapping_table.get(lpn)
        if (
            old_ppn is None
            and self.config.logical_pages is not None
            and len(self.metadata.mapping_table) >= self.config.logical_pages
        ):
            raise ValueError(
                "write would exceed configured logical mapping capacity: "
                f"capacity={self.config.logical_pages}, LPN={lpn}"
            )
        if old_ppn is not None:
            self.metadata.invalidate_old_page(old_ppn)

        self._maybe_run_gc()

        # A threshold attempt can legitimately find no victim. Allocation is
        # still safe while at least one free page remains.
        if self.metadata.free_page_count == 0:
            self.run_gc()

        new_ppn = self.metadata.allocate_free_page()
        self.metadata.register_valid_page(lpn, new_ppn)

        self.host_write_pages += 1
        self.nand_write_pages += 1

        if self.debug_check:
            self.assert_consistent()
        return new_ppn

    def process_write_request(self, lba: int, size: int) -> None:
        """Split one page-based host request into individual LPN writes."""
        self._validate_request(lba, size)
        self._validate_new_mapping_capacity(range(lba, lba + size))
        self.host_write_requests += 1
        for lpn in range(lba, lba + size):
            self.write_lpn(lpn)

    def process_read_request(self, lba: int, size: int) -> None:
        """Count one host read request; timing and NAND reads are out of v1 scope."""
        self._validate_request(lba, size)
        self.host_read_requests += 1
        self.host_read_pages += size

    def process_trace_row(self, row: TraceRow) -> None:
        """Process one already-standardized trace row."""
        try:
            op = str(row["op"]).strip().upper()
            lba = int(row["lba"])
            size = int(row["size"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid trace row: {row!r}") from exc

        if op == "W":
            self.process_write_request(lba, size)
        elif op == "R":
            self.process_read_request(lba, size)
        else:
            raise ValueError(f"trace op must be 'R' or 'W', got {op!r}")

    def run_trace(self, input_path: PathLike) -> Dict[str, Any]:
        """Execute every row in a standard trace and return the cumulative summary."""
        for row in iter_standard_trace(input_path):
            self.process_trace_row(row)
        return self.get_summary()

    def run_trace_rounds(self, input_path: PathLike, rounds: int) -> Dict[str, Any]:
        """Replay one trace multiple times without resetting the SSD state.

        Replaying the same address stream is the v1 preconditioning mechanism.
        Mapping, free-page state, erase counts, and GC history persist between
        rounds, which lets a later measurement phase start from a warmed device.
        """
        if rounds < 0:
            raise ValueError(f"rounds must be >= 0: {rounds}")
        for _ in range(rounds):
            self.run_trace(input_path)
        return self.get_summary()

    def capture_counter_snapshot(self) -> FTLCounterSnapshot:
        """Capture cumulative counters at a warm-up/measurement boundary."""
        total_erases = sum(info.erase_count for info in self.metadata.block_info)
        return FTLCounterSnapshot(
            host_write_requests=self.host_write_requests,
            host_write_pages=self.host_write_pages,
            host_read_requests=self.host_read_requests,
            host_read_pages=self.host_read_pages,
            nand_write_pages=self.nand_write_pages,
            valid_page_migrations=self.valid_page_migrations,
            gc_count=self.gc_count,
            erase_count=total_erases,
            gc_log_length=len(self.gc_log),
        )

    def get_summary_since(
        self, snapshot: FTLCounterSnapshot
    ) -> Dict[str, Any]:
        """Return phase-event deltas and the current end-of-phase state.

        Event counters such as host writes, migrations, GC, and erases are
        differences from ``snapshot``. State gauges such as mapped, free, valid,
        and invalid pages describe the SSD at the moment this method is called.
        """
        current = self.capture_counter_snapshot()
        fields = (
            "host_write_requests",
            "host_write_pages",
            "host_read_requests",
            "host_read_pages",
            "nand_write_pages",
            "valid_page_migrations",
            "gc_count",
            "erase_count",
            "gc_log_length",
        )
        for field in fields:
            if getattr(current, field) < getattr(snapshot, field):
                raise ValueError(
                    "snapshot is newer than the current FTL counters: "
                    f"{field}"
                )

        host_write_pages = current.host_write_pages - snapshot.host_write_pages
        nand_write_pages = current.nand_write_pages - snapshot.nand_write_pages
        waf = (
            nand_write_pages / host_write_pages
            if host_write_pages
            else 0.0
        )
        logical_capacity = self.config.logical_pages
        utilization = (
            len(self.metadata.mapping_table) / logical_capacity
            if logical_capacity
            else 0.0
        )
        return {
            "host_write_requests": current.host_write_requests
            - snapshot.host_write_requests,
            "host_write_pages": host_write_pages,
            "host_read_requests": current.host_read_requests
            - snapshot.host_read_requests,
            "host_read_pages": current.host_read_pages
            - snapshot.host_read_pages,
            "nand_write_pages": nand_write_pages,
            "valid_page_migrations": current.valid_page_migrations
            - snapshot.valid_page_migrations,
            "gc_count": current.gc_count - snapshot.gc_count,
            "erase_count": current.erase_count - snapshot.erase_count,
            "write_amplification_factor": waf,
            "mapped_logical_pages": len(self.metadata.mapping_table),
            "logical_capacity_pages": logical_capacity,
            "logical_capacity_utilization": utilization,
            "free_pages": self.metadata.free_page_count,
            "valid_pages": self.metadata.valid_page_count,
            "invalid_pages": self.metadata.invalid_page_count,
        }

    def get_gc_log_since(
        self, snapshot: FTLCounterSnapshot
    ) -> List[Dict[str, int]]:
        """Return only GC records appended after ``snapshot``."""
        if snapshot.gc_log_length > len(self.gc_log):
            raise ValueError("snapshot GC log position exceeds current log length")
        return [
            entry.to_dict()
            for entry in self.gc_log[snapshot.gc_log_length :]
        ]

    @property
    def write_amplification_factor(self) -> float:
        if self.host_write_pages == 0:
            return 0.0
        return self.nand_write_pages / self.host_write_pages

    def get_summary(self) -> Dict[str, Any]:
        """Return stable experiment counters suitable for CSV/JSON output."""
        total_erases = sum(info.erase_count for info in self.metadata.block_info)
        logical_capacity = self.config.logical_pages
        utilization = (
            len(self.metadata.mapping_table) / logical_capacity
            if logical_capacity
            else 0.0
        )
        return {
            "host_write_requests": self.host_write_requests,
            "host_write_pages": self.host_write_pages,
            "host_read_requests": self.host_read_requests,
            "host_read_pages": self.host_read_pages,
            "nand_write_pages": self.nand_write_pages,
            "valid_page_migrations": self.valid_page_migrations,
            "gc_count": self.gc_count,
            "erase_count": total_erases,
            "write_amplification_factor": self.write_amplification_factor,
            "mapped_logical_pages": len(self.metadata.mapping_table),
            "logical_capacity_pages": logical_capacity,
            "logical_capacity_utilization": utilization,
            "free_pages": self.metadata.free_page_count,
            "valid_pages": self.metadata.valid_page_count,
            "invalid_pages": self.metadata.invalid_page_count,
        }

    def get_gc_log(self) -> List[Dict[str, int]]:
        return [entry.to_dict() for entry in self.gc_log]

    def assert_consistent(self) -> None:
        """Verify metadata and FTL-level accounting invariants."""
        self.metadata.assert_consistent()

        expected_nand_writes = self.host_write_pages + self.valid_page_migrations
        if self.nand_write_pages != expected_nand_writes:
            raise BaselineFTLError(
                "NAND-write accounting mismatch: "
                f"nand={self.nand_write_pages}, host={self.host_write_pages}, "
                f"migration={self.valid_page_migrations}"
            )
        if self.gc_count != len(self.gc_log):
            raise BaselineFTLError(
                f"GC-count mismatch: counter={self.gc_count}, log={len(self.gc_log)}"
            )
        total_erases = sum(info.erase_count for info in self.metadata.block_info)
        if total_erases != self.gc_count:
            raise BaselineFTLError(
                f"erase-count mismatch: erases={total_erases}, GC={self.gc_count}"
            )
