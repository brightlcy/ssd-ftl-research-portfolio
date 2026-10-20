from __future__ import annotations

from dataclasses import asdict, dataclass
from math import ceil
from typing import Any


@dataclass(frozen=True)
class SSDConfig:
    """Resolved SSD geometry and GC safety configuration.

    `logical_pages` is the host-visible logical capacity in pages. It is a
    capacity count and does not require sparse external LPN identifiers to fit
    in the numeric interval [0, logical_pages).
    Physical capacity is larger than logical capacity so that GC has spare space.
    """

    page_size: int
    chips: int
    blocks_per_chip: int
    pages_per_block: int
    gc_threshold_ratio: float = 0.05
    reserved_gc_blocks: int = 2
    logical_pages: int | None = None
    target_overprovision_ratio: float | None = None
    capacity_basis: str = "manual"

    def __post_init__(self) -> None:
        positive_fields = {
            "page_size": self.page_size,
            "chips": self.chips,
            "blocks_per_chip": self.blocks_per_chip,
            "pages_per_block": self.pages_per_block,
        }
        for name, value in positive_fields.items():
            if value <= 0:
                raise ValueError(f"{name} must be greater than 0: {value}")

        if not 0.0 < self.gc_threshold_ratio < 1.0:
            raise ValueError(
                "gc_threshold_ratio must be between 0 and 1 (exclusive): "
                f"{self.gc_threshold_ratio}"
            )

        if self.reserved_gc_blocks < 1:
            raise ValueError("reserved_gc_blocks must be at least 1")
        if self.reserved_gc_blocks >= self.total_blocks:
            raise ValueError(
                "reserved_gc_blocks must be smaller than total_blocks: "
                f"{self.reserved_gc_blocks} >= {self.total_blocks}"
            )

        if self.logical_pages is not None:
            if self.logical_pages <= 0:
                raise ValueError("logical_pages must be greater than 0")
            if self.logical_pages + self.gc_reserve_pages > self.total_pages:
                raise ValueError(
                    "physical capacity is too small for logical pages and the GC reserve: "
                    f"logical={self.logical_pages}, reserve={self.gc_reserve_pages}, "
                    f"physical={self.total_pages}"
                )

        if not self.capacity_basis.strip():
            raise ValueError("capacity_basis must not be empty")

        if self.target_overprovision_ratio is not None:
            if not 0.0 < self.target_overprovision_ratio < 1.0:
                raise ValueError(
                    "target_overprovision_ratio must be between 0 and 1 (exclusive): "
                    f"{self.target_overprovision_ratio}"
                )

    @property
    def total_blocks(self) -> int:
        return self.chips * self.blocks_per_chip

    @property
    def total_pages(self) -> int:
        return self.total_blocks * self.pages_per_block

    @property
    def total_bytes(self) -> int:
        return self.total_pages * self.page_size

    @property
    def pages_per_chip(self) -> int:
        return self.blocks_per_chip * self.pages_per_block

    @property
    def gc_reserve_pages(self) -> int:
        return self.reserved_gc_blocks * self.pages_per_block

    @property
    def gc_threshold_pages(self) -> int:
        ratio_threshold = ceil(self.total_pages * self.gc_threshold_ratio)
        return max(ratio_threshold, self.gc_reserve_pages)

    @property
    def actual_overprovision_ratio(self) -> float | None:
        if self.logical_pages is None:
            return None
        return (self.total_pages - self.logical_pages) / self.total_pages

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.update(
            {
                "total_blocks": self.total_blocks,
                "total_pages": self.total_pages,
                "total_bytes": self.total_bytes,
                "gc_reserve_pages": self.gc_reserve_pages,
                "gc_threshold_pages": self.gc_threshold_pages,
                "actual_overprovision_ratio": self.actual_overprovision_ratio,
            }
        )
        return data

    @classmethod
    def from_logical_pages(
        cls,
        *,
        logical_pages: int,
        page_size: int = 4096,
        chips: int = 4,
        pages_per_block: int = 64,
        overprovision_ratio: float = 0.20,
        gc_threshold_ratio: float = 0.05,
        reserved_gc_blocks: int = 2,
        capacity_basis: str = "manual",
    ) -> "SSDConfig":
        """Resolve SSD size from a trace's logical address-space size.

        Capacity is rounded up in units of one block per chip, so every chip has
        the same number of blocks. The result also guarantees dedicated GC space.
        """
        if logical_pages <= 0:
            raise ValueError("logical_pages must be greater than 0")
        if page_size <= 0 or chips <= 0 or pages_per_block <= 0:
            raise ValueError("page_size, chips, and pages_per_block must be positive")
        if not 0.0 < overprovision_ratio < 1.0:
            raise ValueError("overprovision_ratio must be between 0 and 1 (exclusive)")
        if reserved_gc_blocks < 1:
            raise ValueError("reserved_gc_blocks must be at least 1")

        capacity_for_target_op = ceil(logical_pages / (1.0 - overprovision_ratio))
        capacity_for_gc_safety = logical_pages + reserved_gc_blocks * pages_per_block
        required_physical_pages = max(capacity_for_target_op, capacity_for_gc_safety)

        pages_per_chip_block_stripe = chips * pages_per_block
        blocks_per_chip = ceil(required_physical_pages / pages_per_chip_block_stripe)

        return cls(
            page_size=page_size,
            chips=chips,
            blocks_per_chip=blocks_per_chip,
            pages_per_block=pages_per_block,
            gc_threshold_ratio=gc_threshold_ratio,
            reserved_gc_blocks=reserved_gc_blocks,
            logical_pages=logical_pages,
            target_overprovision_ratio=overprovision_ratio,
            capacity_basis=capacity_basis,
        )
