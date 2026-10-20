from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Any, Deque, Dict, List, Optional, Tuple

from .address import ppn_to_block
from .baseline_ftl import (
    BaselineFTL,
    BaselineFTLError,
    FTLCounterSnapshot,
    GCLogEntry,
    InsufficientGCWorkspaceError,
)
from .config import SSDConfig
from .metadata import FTLMetadata, NoFreePageError


class DataTemperature(str, Enum):
    COLD = "cold"
    HOT = "hot"


class OnlineHotnessTracker:
    """Classify LPNs using only writes observed so far.

    A global sliding window measured in host page writes keeps memory bounded and
    avoids trace-time assumptions. The current write is included before the
    classification is returned, so the first write is cold and the Nth recent
    write becomes hot when ``hot_threshold`` equals N.
    """

    def __init__(self, *, hot_threshold: int = 2, window_writes: int = 4096) -> None:
        if hot_threshold < 2:
            raise ValueError(f"hot_threshold must be >= 2: {hot_threshold}")
        if window_writes < hot_threshold:
            raise ValueError(
                "window_writes must be >= hot_threshold: "
                f"{window_writes} < {hot_threshold}"
            )
        self.hot_threshold = hot_threshold
        self.window_writes = window_writes
        self.sequence = 0
        self._events: Deque[Tuple[int, int]] = deque()
        self._counts: Dict[int, int] = {}
        self._hot_lpns: set[int] = set()
        self.promotions = 0
        self.demotions = 0

    def _expire(self) -> None:
        minimum_sequence = self.sequence - self.window_writes + 1
        while self._events and self._events[0][0] < minimum_sequence:
            _, lpn = self._events.popleft()
            new_count = self._counts[lpn] - 1
            if new_count == 0:
                del self._counts[lpn]
            else:
                self._counts[lpn] = new_count
            if lpn in self._hot_lpns and new_count < self.hot_threshold:
                self._hot_lpns.remove(lpn)
                self.demotions += 1

    def observe_write(self, lpn: int) -> DataTemperature:
        if lpn < 0:
            raise ValueError(f"LPN must be >= 0: {lpn}")
        self.sequence += 1
        self._expire()
        self._events.append((self.sequence, lpn))
        count = self._counts.get(lpn, 0) + 1
        self._counts[lpn] = count
        if count >= self.hot_threshold and lpn not in self._hot_lpns:
            self._hot_lpns.add(lpn)
            self.promotions += 1
        return self.classify(lpn)

    def classify(self, lpn: int) -> DataTemperature:
        self._expire()
        return DataTemperature.HOT if lpn in self._hot_lpns else DataTemperature.COLD

    @property
    def tracked_recent_lpns(self) -> int:
        return len(self._counts)

    @property
    def recent_event_count(self) -> int:
        return len(self._events)

    @property
    def hot_lpn_count(self) -> int:
        return len(self._hot_lpns)


@dataclass(frozen=True)
class HotColdCounterSnapshot(FTLCounterSnapshot):
    hot_host_write_pages: int
    cold_host_write_pages: int
    hot_gc_migration_pages: int
    cold_gc_migration_pages: int
    placement_fallback_pages: int
    promotions: int
    demotions: int


class HotColdFTL(BaselineFTL):
    """Online hot/cold page placement on top of the proven baseline FTL.

    Only placement changes. GC threshold, greedy victim selection, capacity,
    warm-up semantics, and accounting stay equal to the baseline. Two dynamic
    write frontiers draw blocks from one shared free-block pool, avoiding a fixed
    hot/cold partition that could strand capacity under workload imbalance.
    """

    def __init__(
        self,
        config: SSDConfig,
        *,
        metadata: Optional[FTLMetadata] = None,
        debug_check: bool = False,
        hot_threshold: int = 2,
        hot_window_writes: int = 4096,
    ) -> None:
        super().__init__(config, metadata=metadata, debug_check=debug_check)
        self.hotness = OnlineHotnessTracker(
            hot_threshold=hot_threshold,
            window_writes=hot_window_writes,
        )
        self.active_blocks: Dict[DataTemperature, Optional[int]] = {
            DataTemperature.COLD: None,
            DataTemperature.HOT: None,
        }
        self.block_temperature: Dict[int, str] = {}
        self.hot_host_write_pages = 0
        self.cold_host_write_pages = 0
        self.hot_gc_migration_pages = 0
        self.cold_gc_migration_pages = 0
        self.placement_fallback_pages = 0

    def _usable_active_block(self, temperature: DataTemperature) -> Optional[int]:
        block_id = self.active_blocks[temperature]
        if block_id is None:
            return None
        if self.metadata.get_block_info(block_id).free_count > 0:
            return block_id
        self.active_blocks[temperature] = None
        return None

    def _claim_free_block(self, temperature: DataTemperature) -> Optional[int]:
        active = {block for block in self.active_blocks.values() if block is not None}
        for block_id in self.metadata.get_fully_free_blocks():
            if block_id in active:
                continue
            self.active_blocks[temperature] = block_id
            self.block_temperature[block_id] = temperature.value
            return block_id
        return None

    def _allocate_by_temperature(self, temperature: DataTemperature) -> int:
        block_id = self._usable_active_block(temperature)
        if block_id is None:
            block_id = self._claim_free_block(temperature)
        if block_id is not None:
            ppn = self.metadata.allocate_free_page_in_block(block_id)
            if self.metadata.get_block_info(block_id).free_count == 0:
                self.active_blocks[temperature] = None
            return ppn

        # Safety fallback: when all free pages are trapped in partial frontiers,
        # preserve forward progress rather than failing the experiment. The event
        # is counted so a policy result cannot hide placement-purity loss.
        ppn = self.metadata.allocate_free_page()
        fallback_block = ppn_to_block(ppn, self.config)
        prior = self.block_temperature.get(fallback_block)
        if prior is None:
            self.block_temperature[fallback_block] = temperature.value
            self.active_blocks[temperature] = fallback_block
        elif prior != temperature.value:
            self.block_temperature[fallback_block] = "mixed"
        self.placement_fallback_pages += 1
        return ppn

    def run_gc(self) -> GCLogEntry:
        candidate = self._select_victim()
        victim_block = candidate.block_id
        valid_pages = self.metadata.get_valid_pages_in_block(victim_block)

        destination_ppns: List[int] = []
        destination_temperatures: List[DataTemperature] = []
        try:
            for _, lpn in valid_pages:
                temperature = self.hotness.classify(lpn)
                destination_temperatures.append(temperature)
                destination_ppns.append(self._allocate_by_temperature(temperature))
        except NoFreePageError as exc:
            for ppn in destination_ppns:
                self.metadata.release_reserved_page(ppn)
            raise InsufficientGCWorkspaceError(
                "free-page pool changed while reserving hot/cold GC destinations"
            ) from exc

        free_pages_before = self.metadata.free_page_count + len(destination_ppns)
        migrated_pages = 0
        for (old_ppn, lpn), new_ppn, temperature in zip(
            valid_pages, destination_ppns, destination_temperatures
        ):
            if self.metadata.mapping_table.get(lpn) != old_ppn:
                raise BaselineFTLError(
                    "mapping changed during hot/cold GC migration: "
                    f"LPN {lpn}, expected PPN {old_ppn}"
                )
            self.metadata.invalidate_old_page(old_ppn)
            self.metadata.register_valid_page(lpn, new_ppn)
            migrated_pages += 1
            self.valid_page_migrations += 1
            self.nand_write_pages += 1
            if temperature is DataTemperature.HOT:
                self.hot_gc_migration_pages += 1
            else:
                self.cold_gc_migration_pages += 1

        self.metadata.erase_block(victim_block)
        self.block_temperature.pop(victim_block, None)
        for temperature, active_block in tuple(self.active_blocks.items()):
            if active_block == victim_block:
                self.active_blocks[temperature] = None
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

    def write_lpn(self, lpn: int) -> int:
        self._validate_lpn(lpn)
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

        temperature = self.hotness.observe_write(lpn)
        if old_ppn is not None:
            self.metadata.invalidate_old_page(old_ppn)
        self._maybe_run_gc()
        if self.metadata.free_page_count == 0:
            self.run_gc()

        new_ppn = self._allocate_by_temperature(temperature)
        self.metadata.register_valid_page(lpn, new_ppn)
        self.host_write_pages += 1
        self.nand_write_pages += 1
        if temperature is DataTemperature.HOT:
            self.hot_host_write_pages += 1
        else:
            self.cold_host_write_pages += 1
        if self.debug_check:
            self.assert_consistent()
        return new_ppn

    def capture_counter_snapshot(self) -> HotColdCounterSnapshot:
        base = super().capture_counter_snapshot()
        return HotColdCounterSnapshot(
            **base.__dict__,
            hot_host_write_pages=self.hot_host_write_pages,
            cold_host_write_pages=self.cold_host_write_pages,
            hot_gc_migration_pages=self.hot_gc_migration_pages,
            cold_gc_migration_pages=self.cold_gc_migration_pages,
            placement_fallback_pages=self.placement_fallback_pages,
            promotions=self.hotness.promotions,
            demotions=self.hotness.demotions,
        )

    def _policy_state(self) -> Dict[str, Any]:
        counts = {"hot": 0, "cold": 0, "mixed": 0}
        for label in self.block_temperature.values():
            counts[label] = counts.get(label, 0) + 1
        return {
            "hot_threshold": self.hotness.hot_threshold,
            "hot_window_writes": self.hotness.window_writes,
            "tracked_recent_lpns": self.hotness.tracked_recent_lpns,
            "recent_hotness_events": self.hotness.recent_event_count,
            "hot_lpn_count": self.hotness.hot_lpn_count,
            "hot_labeled_blocks": counts.get("hot", 0),
            "cold_labeled_blocks": counts.get("cold", 0),
            "mixed_labeled_blocks": counts.get("mixed", 0),
        }

    def get_summary_since(self, snapshot: FTLCounterSnapshot) -> Dict[str, Any]:
        summary = super().get_summary_since(snapshot)
        if not isinstance(snapshot, HotColdCounterSnapshot):
            raise TypeError("HotColdFTL requires a HotColdCounterSnapshot")
        current = self.capture_counter_snapshot()
        summary.update(
            {
                "hot_host_write_pages": current.hot_host_write_pages
                - snapshot.hot_host_write_pages,
                "cold_host_write_pages": current.cold_host_write_pages
                - snapshot.cold_host_write_pages,
                "hot_gc_migration_pages": current.hot_gc_migration_pages
                - snapshot.hot_gc_migration_pages,
                "cold_gc_migration_pages": current.cold_gc_migration_pages
                - snapshot.cold_gc_migration_pages,
                "placement_fallback_pages": current.placement_fallback_pages
                - snapshot.placement_fallback_pages,
                "hot_promotions": current.promotions - snapshot.promotions,
                "hot_demotions": current.demotions - snapshot.demotions,
            }
        )
        summary.update(self._policy_state())
        return summary

    def get_summary(self) -> Dict[str, Any]:
        summary = super().get_summary()
        summary.update(
            {
                "hot_host_write_pages": self.hot_host_write_pages,
                "cold_host_write_pages": self.cold_host_write_pages,
                "hot_gc_migration_pages": self.hot_gc_migration_pages,
                "cold_gc_migration_pages": self.cold_gc_migration_pages,
                "placement_fallback_pages": self.placement_fallback_pages,
                "hot_promotions": self.hotness.promotions,
                "hot_demotions": self.hotness.demotions,
            }
        )
        summary.update(self._policy_state())
        return summary
