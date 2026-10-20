from __future__ import annotations

from typing import Iterator

from .config import SSDConfig


def _validate_ppn(ppn: int, config: SSDConfig) -> None:
    if not 0 <= ppn < config.total_pages:
        raise ValueError(f"PPN out of range: {ppn}; valid=0..{config.total_pages - 1}")


def _validate_block_id(block_id: int, config: SSDConfig) -> None:
    if not 0 <= block_id < config.total_blocks:
        raise ValueError(
            f"block_id out of range: {block_id}; valid=0..{config.total_blocks - 1}"
        )


def ppn_to_block(ppn: int, config: SSDConfig) -> int:
    """Return the global block id containing `ppn`."""
    _validate_ppn(ppn, config)
    return ppn // config.pages_per_block


def decode_ppn(ppn: int, config: SSDConfig) -> tuple[int, int, int]:
    """Decode a flat PPN into `(chip, block_in_chip, page_in_block)`."""
    _validate_ppn(ppn, config)

    page = ppn % config.pages_per_block
    global_block = ppn // config.pages_per_block
    block_in_chip = global_block % config.blocks_per_chip
    chip = global_block // config.blocks_per_chip
    return chip, block_in_chip, page


def encode_ppn(
    chip: int,
    block_in_chip: int,
    page_in_block: int,
    config: SSDConfig,
) -> int:
    """Encode `(chip, block_in_chip, page_in_block)` into a flat PPN."""
    if not 0 <= chip < config.chips:
        raise ValueError(f"chip out of range: {chip}; valid=0..{config.chips - 1}")
    if not 0 <= block_in_chip < config.blocks_per_chip:
        raise ValueError(
            "block_in_chip out of range: "
            f"{block_in_chip}; valid=0..{config.blocks_per_chip - 1}"
        )
    if not 0 <= page_in_block < config.pages_per_block:
        raise ValueError(
            "page_in_block out of range: "
            f"{page_in_block}; valid=0..{config.pages_per_block - 1}"
        )

    return (
        (chip * config.blocks_per_chip + block_in_chip) * config.pages_per_block
        + page_in_block
    )


def block_page_range(block_id: int, config: SSDConfig) -> range:
    """Return all flat PPNs that belong to a global block id."""
    _validate_block_id(block_id, config)
    start = block_id * config.pages_per_block
    return range(start, start + config.pages_per_block)


def iter_chip_ppns(chip: int, config: SSDConfig) -> Iterator[int]:
    """Iterate all PPNs belonging to one chip."""
    if not 0 <= chip < config.chips:
        raise ValueError(f"chip out of range: {chip}; valid=0..{config.chips - 1}")
    start = chip * config.pages_per_chip
    yield from range(start, start + config.pages_per_chip)
