from __future__ import annotations

from dataclasses import dataclass
from typing import FrozenSet, Iterable, List, Optional, Tuple

from .metadata import FTLMetadata


class NoVictimBlockError(RuntimeError):
    """Raised when GC has no block that can be reclaimed safely."""


@dataclass(frozen=True)
class VictimCandidate:
    """Immutable snapshot of one block considered by the GC policy."""

    block_id: int
    valid_pages: int
    invalid_pages: int
    free_pages: int
    erase_count: int

    @property
    def migration_cost(self) -> int:
        """Number of valid pages that GC must copy before erase."""
        return self.valid_pages

    @property
    def reclaimable_pages(self) -> int:
        """Pages reclaimed immediately by erasing this closed block."""
        return self.invalid_pages

    @property
    def invalid_ratio(self) -> float:
        programmed_pages = self.valid_pages + self.invalid_pages
        if programmed_pages == 0:
            return 0.0
        return self.invalid_pages / programmed_pages


class GreedyGCPolicy:
    """Select a closed block with the largest number of invalid pages.

    Baseline v1 uses deterministic greedy victim selection:

      1. Exclude blocks named by the caller.
      2. Exclude blocks that are FREE, partially written, or RESERVED.
      3. Exclude blocks with zero invalid pages because erasing them reclaims
         no stale data and would only copy valid pages.
      4. Maximize invalid-page count (equivalent to minimizing valid-page
         migration cost for equally sized closed blocks).
      5. Break ties by fewer erase cycles, then lower global block id.

    The wear-aware tie break does not change the primary greedy objective. It
    only makes an otherwise equal decision deterministic and avoids repeatedly
    selecting a more-worn block when another equal candidate exists.
    """

    def __init__(self, *, require_closed_block: bool = True) -> None:
        self.require_closed_block = require_closed_block

    def is_eligible(
        self,
        metadata: FTLMetadata,
        block_id: int,
        *,
        excluded_block_ids: Optional[Iterable[int]] = None,
    ) -> bool:
        """Return whether a block can be considered as a GC victim."""
        excluded: FrozenSet[int] = frozenset(excluded_block_ids or ())
        if block_id in excluded:
            return False

        info = metadata.get_block_info(block_id)

        # GC must never erase a block while a page allocation is in flight.
        if info.reserved_count != 0:
            return False

        # A block without stale pages gives no useful reclamation.
        if info.invalid_count <= 0:
            return False

        # Baseline v1 only collects closed blocks. This protects the current
        # write frontier and makes victim accounting easy to reason about.
        if self.require_closed_block and info.free_count != 0:
            return False

        return True

    def get_candidates(
        self,
        metadata: FTLMetadata,
        *,
        excluded_block_ids: Optional[Iterable[int]] = None,
    ) -> List[VictimCandidate]:
        """Return deterministic candidate snapshots ordered by block id."""
        candidates: List[VictimCandidate] = []
        for block_id in range(metadata.config.total_blocks):
            if not self.is_eligible(
                metadata,
                block_id,
                excluded_block_ids=excluded_block_ids,
            ):
                continue

            info = metadata.get_block_info(block_id)
            candidates.append(
                VictimCandidate(
                    block_id=block_id,
                    valid_pages=info.valid_count,
                    invalid_pages=info.invalid_count,
                    free_pages=info.free_count,
                    erase_count=info.erase_count,
                )
            )
        return candidates

    @staticmethod
    def _selection_key(candidate: VictimCandidate) -> Tuple[int, int, int]:
        """Return a key where the best candidate has the greatest value."""
        return (
            candidate.invalid_pages,
            -candidate.erase_count,
            -candidate.block_id,
        )

    def select_victim(
        self,
        metadata: FTLMetadata,
        *,
        excluded_block_ids: Optional[Iterable[int]] = None,
    ) -> VictimCandidate:
        """Return the best victim candidate or raise NoVictimBlockError."""
        candidates = self.get_candidates(
            metadata,
            excluded_block_ids=excluded_block_ids,
        )
        if not candidates:
            raise NoVictimBlockError(
                "no eligible GC victim: a closed block with at least one "
                "invalid page is required"
            )
        return max(candidates, key=self._selection_key)

    def select_victim_block(
        self,
        metadata: FTLMetadata,
        *,
        excluded_block_ids: Optional[Iterable[int]] = None,
    ) -> int:
        """Compatibility helper returning only the global block id."""
        return self.select_victim(
            metadata,
            excluded_block_ids=excluded_block_ids,
        ).block_id
