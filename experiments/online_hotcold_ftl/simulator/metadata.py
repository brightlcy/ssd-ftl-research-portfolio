from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Deque, Dict, List, Set, Tuple

from .address import block_page_range, ppn_to_block
from .config import SSDConfig


class PageState(str, Enum):
    """Logical state of one physical NAND page.

    RESERVED is a short-lived internal state between allocation and programming.
    It prevents the allocator from returning the same page twice.
    """

    FREE = "FREE"
    RESERVED = "RESERVED"
    VALID = "VALID"
    INVALID = "INVALID"


class NoFreePageError(RuntimeError):
    """Raised when no allocatable physical page remains."""


class MetadataStateError(RuntimeError):
    """Raised when an operation would violate FTL metadata invariants."""


@dataclass
class BlockInfo:
    """Summary counters for one global physical block."""

    valid_count: int
    invalid_count: int
    free_count: int
    reserved_count: int = 0
    erase_count: int = 0

    @property
    def programmed_count(self) -> int:
        """Pages that have been programmed at least once since the last erase."""
        return self.valid_count + self.invalid_count

    def to_dict(self) -> Dict[str, int]:
        return asdict(self)


class FTLMetadata:
    """Own the in-memory metadata used by the baseline page-mapping FTL.

    Main indexes:
      * mapping_table: LPN -> current valid PPN
      * page_owner:    current valid PPN -> LPN
      * free_page_set: O(1) FREE membership test
      * free_page_queue: deterministic FIFO allocation order
      * reserved_page_set: pages allocated but not programmed yet
      * block_info: per-block valid/invalid/free/reserved/erase counters

    INVALID pages do not need a dedicated per-page table. A page that is not
    FREE, RESERVED, or present in page_owner is INVALID.
    """

    def __init__(self, config: SSDConfig) -> None:
        self.config = config

        self.mapping_table: Dict[int, int] = {}
        self.page_owner: Dict[int, int] = {}

        all_ppns = range(config.total_pages)
        self.free_page_queue: Deque[int] = deque(all_ppns)
        self.free_page_set: Set[int] = set(range(config.total_pages))
        self.reserved_page_set: Set[int] = set()

        self.block_info: List[BlockInfo] = [
            BlockInfo(
                valid_count=0,
                invalid_count=0,
                free_count=config.pages_per_block,
            )
            for _ in range(config.total_blocks)
        ]

    @property
    def free_page_count(self) -> int:
        return len(self.free_page_set)

    @property
    def valid_page_count(self) -> int:
        return len(self.page_owner)

    @property
    def reserved_page_count(self) -> int:
        return len(self.reserved_page_set)

    @property
    def invalid_page_count(self) -> int:
        return sum(info.invalid_count for info in self.block_info)

    def get_page_state(self, ppn: int) -> PageState:
        """Return FREE/RESERVED/VALID/INVALID for one PPN."""
        # ppn_to_block performs the range check for us.
        ppn_to_block(ppn, self.config)

        if ppn in self.free_page_set:
            return PageState.FREE
        if ppn in self.reserved_page_set:
            return PageState.RESERVED
        if ppn in self.page_owner:
            return PageState.VALID
        return PageState.INVALID

    def _reserve_free_page(self, ppn: int) -> int:
        """Move one known FREE page into RESERVED state and return it."""
        state = self.get_page_state(ppn)
        if state is not PageState.FREE:
            raise MetadataStateError(
                f"cannot reserve PPN {ppn}: expected FREE, found {state.value}"
            )

        self.free_page_set.remove(ppn)
        self.reserved_page_set.add(ppn)

        block_id = ppn_to_block(ppn, self.config)
        info = self.block_info[block_id]
        if info.free_count <= 0:
            raise MetadataStateError(
                f"block {block_id} free_count underflow while allocating PPN {ppn}"
            )
        info.free_count -= 1
        info.reserved_count += 1
        return ppn

    def allocate_free_page(self) -> int:
        """Reserve and return one free PPN in deterministic FIFO order.

        Allocation immediately removes the page from the free pool. The caller
        must then either register_valid_page() or release_reserved_page().
        """
        while self.free_page_queue:
            ppn = self.free_page_queue.popleft()

            # Defensive handling for stale queue entries. Normal operations keep
            # queue and set synchronized, but this avoids double allocation even
            # if a future policy changes queue maintenance.
            if ppn not in self.free_page_set:
                continue
            return self._reserve_free_page(ppn)

        raise NoFreePageError("no free physical page remains; GC must run first")

    def allocate_free_page_in_block(self, block_id: int) -> int:
        """Reserve the lowest-numbered FREE page in one physical block.

        Policy implementations use this method to keep independent write
        frontiers, while the baseline continues to use global FIFO allocation.
        """
        for ppn in block_page_range(block_id, self.config):
            if ppn not in self.free_page_set:
                continue
            # Remove the entry from the FIFO queue so the global allocator cannot
            # return the same page later. This is O(total free pages), acceptable
            # for the policy-prototype scale and kept explicit for correctness.
            self.free_page_queue = deque(
                candidate
                for candidate in self.free_page_queue
                if candidate != ppn
            )
            return self._reserve_free_page(ppn)
        raise NoFreePageError(f"block {block_id} has no free physical page")

    def get_fully_free_blocks(self) -> List[int]:
        """Return erased/unwritten block ids in deterministic order."""
        return [
            block_id
            for block_id, info in enumerate(self.block_info)
            if info.free_count == self.config.pages_per_block
            and info.valid_count == 0
            and info.invalid_count == 0
            and info.reserved_count == 0
        ]

    def release_reserved_page(self, ppn: int) -> None:
        """Return an allocated-but-unprogrammed PPN to the free pool."""
        state = self.get_page_state(ppn)
        if state is not PageState.RESERVED:
            raise MetadataStateError(
                f"cannot release PPN {ppn}: expected RESERVED, found {state.value}"
            )

        self.reserved_page_set.remove(ppn)
        self.free_page_set.add(ppn)
        self.free_page_queue.append(ppn)

        block_id = ppn_to_block(ppn, self.config)
        info = self.block_info[block_id]
        info.reserved_count -= 1
        info.free_count += 1

    def register_valid_page(self, lpn: int, ppn: int) -> None:
        """Program a reserved PPN and make it the current location of an LPN.

        Existing LPN mappings must be invalidated explicitly first. Keeping that
        transition separate makes overwrite accounting visible and testable.
        """
        if lpn < 0:
            raise ValueError(f"LPN must be >= 0: {lpn}")

        state = self.get_page_state(ppn)
        if state is not PageState.RESERVED:
            raise MetadataStateError(
                f"cannot register PPN {ppn}: expected RESERVED, found {state.value}"
            )
        if lpn in self.mapping_table:
            old_ppn = self.mapping_table[lpn]
            raise MetadataStateError(
                f"LPN {lpn} is already mapped to PPN {old_ppn}; "
                "invalidate the old page before registering a new one"
            )

        self.reserved_page_set.remove(ppn)
        self.mapping_table[lpn] = ppn
        self.page_owner[ppn] = lpn

        block_id = ppn_to_block(ppn, self.config)
        info = self.block_info[block_id]
        info.reserved_count -= 1
        info.valid_count += 1

    def invalidate_old_page(self, old_ppn: int) -> int:
        """Invalidate one currently valid PPN and return its former LPN."""
        state = self.get_page_state(old_ppn)
        if state is not PageState.VALID:
            raise MetadataStateError(
                f"cannot invalidate PPN {old_ppn}: expected VALID, found {state.value}"
            )

        lpn = self.page_owner.pop(old_ppn)
        mapped_ppn = self.mapping_table.get(lpn)
        if mapped_ppn != old_ppn:
            raise MetadataStateError(
                "mapping_table/page_owner mismatch while invalidating: "
                f"LPN {lpn}, mapping_table={mapped_ppn}, page_owner PPN={old_ppn}"
            )
        del self.mapping_table[lpn]

        block_id = ppn_to_block(old_ppn, self.config)
        info = self.block_info[block_id]
        if info.valid_count <= 0:
            raise MetadataStateError(
                f"block {block_id} valid_count underflow while invalidating PPN {old_ppn}"
            )
        info.valid_count -= 1
        info.invalid_count += 1
        return lpn

    def invalidate_lpn(self, lpn: int) -> int:
        """Invalidate the current page of an LPN and return the old PPN."""
        if lpn not in self.mapping_table:
            raise KeyError(f"LPN is not mapped: {lpn}")
        old_ppn = self.mapping_table[lpn]
        self.invalidate_old_page(old_ppn)
        return old_ppn

    def get_valid_pages_in_block(self, block_id: int) -> List[Tuple[int, int]]:
        """Return `(ppn, lpn)` pairs for all valid pages in one block."""
        result: List[Tuple[int, int]] = []
        for ppn in block_page_range(block_id, self.config):
            lpn = self.page_owner.get(ppn)
            if lpn is not None:
                result.append((ppn, lpn))
        return result

    def erase_block(self, block_id: int) -> None:
        """Erase a block after every valid page has been migrated.

        The method also rebuilds queue membership for this block, so partially
        free blocks can be erased safely in tests without creating duplicates.
        """
        block_ppns = list(block_page_range(block_id, self.config))
        info = self.block_info[block_id]
        valid_pages = self.get_valid_pages_in_block(block_id)
        reserved_pages = [ppn for ppn in block_ppns if ppn in self.reserved_page_set]

        if valid_pages:
            raise MetadataStateError(
                f"cannot erase block {block_id}: {len(valid_pages)} valid page(s) remain"
            )
        if reserved_pages:
            raise MetadataStateError(
                f"cannot erase block {block_id}: "
                f"{len(reserved_pages)} reserved page(s) remain"
            )

        block_ppn_set = set(block_ppns)
        self.free_page_queue = deque(
            ppn for ppn in self.free_page_queue if ppn not in block_ppn_set
        )
        self.free_page_set.difference_update(block_ppn_set)

        for ppn in block_ppns:
            self.free_page_set.add(ppn)
            self.free_page_queue.append(ppn)

        erase_count = info.erase_count + 1
        self.block_info[block_id] = BlockInfo(
            valid_count=0,
            invalid_count=0,
            free_count=self.config.pages_per_block,
            reserved_count=0,
            erase_count=erase_count,
        )

    def get_block_info(self, block_id: int) -> BlockInfo:
        """Return the mutable summary object for a global block id."""
        # block_page_range performs range validation.
        block_page_range(block_id, self.config)
        return self.block_info[block_id]

    def to_dict(self) -> Dict[str, int]:
        """Return compact global counters for logs and debugging."""
        return {
            "mapped_logical_pages": len(self.mapping_table),
            "free_pages": self.free_page_count,
            "reserved_pages": self.reserved_page_count,
            "valid_pages": self.valid_page_count,
            "invalid_pages": self.invalid_page_count,
            "total_erases": sum(info.erase_count for info in self.block_info),
        }

    def assert_consistent(self) -> None:
        """Expensively verify all metadata indexes and counters.

        Call this in tests and debug runs, not on every page write in large
        experiments.
        """
        if len(self.free_page_queue) != len(set(self.free_page_queue)):
            raise MetadataStateError("free_page_queue contains duplicate PPNs")
        if set(self.free_page_queue) != self.free_page_set:
            raise MetadataStateError("free_page_queue and free_page_set disagree")

        if self.free_page_set & self.reserved_page_set:
            raise MetadataStateError("FREE and RESERVED page sets overlap")
        if self.free_page_set & set(self.page_owner):
            raise MetadataStateError("FREE and VALID page sets overlap")
        if self.reserved_page_set & set(self.page_owner):
            raise MetadataStateError("RESERVED and VALID page sets overlap")

        if len(self.mapping_table) != len(self.page_owner):
            raise MetadataStateError("mapping_table and page_owner size mismatch")
        for lpn, ppn in self.mapping_table.items():
            if self.page_owner.get(ppn) != lpn:
                raise MetadataStateError(
                    f"inverse mapping mismatch: LPN {lpn} -> PPN {ppn}"
                )
        for ppn, lpn in self.page_owner.items():
            if self.mapping_table.get(lpn) != ppn:
                raise MetadataStateError(
                    f"inverse mapping mismatch: PPN {ppn} -> LPN {lpn}"
                )

        total_counted_pages = 0
        for block_id, info in enumerate(self.block_info):
            observed = {
                PageState.FREE: 0,
                PageState.RESERVED: 0,
                PageState.VALID: 0,
                PageState.INVALID: 0,
            }
            for ppn in block_page_range(block_id, self.config):
                observed[self.get_page_state(ppn)] += 1

            expected = {
                PageState.FREE: info.free_count,
                PageState.RESERVED: info.reserved_count,
                PageState.VALID: info.valid_count,
                PageState.INVALID: info.invalid_count,
            }
            if observed != expected:
                raise MetadataStateError(
                    f"block {block_id} counters disagree: "
                    f"observed={observed}, expected={expected}"
                )

            block_total = (
                info.free_count
                + info.reserved_count
                + info.valid_count
                + info.invalid_count
            )
            if block_total != self.config.pages_per_block:
                raise MetadataStateError(
                    f"block {block_id} page-count mismatch: "
                    f"{block_total} != {self.config.pages_per_block}"
                )
            total_counted_pages += block_total

        if total_counted_pages != self.config.total_pages:
            raise MetadataStateError(
                f"global page-count mismatch: "
                f"{total_counted_pages} != {self.config.total_pages}"
            )
