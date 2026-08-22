from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from app.modules.document.parsers import ParsedDocument


@dataclass(frozen=True)
class NormalizedChunk:
    stable_ref: str
    content_type: str
    content: str
    page_number: int | None
    block_index: int
    bounding_box: list[float] | None
    table_metadata: dict[str, Any]
    extraction_method: str
    extraction_confidence: float | None
    content_hash: str
    token_count: int


def _stable_ref(parsed: ParsedDocument, block_index: int) -> str:
    block = parsed.blocks[block_index - 1]
    if block.table_index is not None and block.row_index is not None:
        if parsed.format == "pdf":
            return (
                f"pdf:page:{block.page_number}:table:{block.table_index}:"
                f"row:{block.row_index}"
            )
        return f"docx:table:{block.table_index}:row:{block.row_index}"
    if parsed.format == "pdf":
        return f"pdf:page:{block.page_number}:block:{block_index}"
    return f"docx:block:{block_index}"


def build_chunks(parsed: ParsedDocument) -> list[NormalizedChunk]:
    chunks: list[NormalizedChunk] = []
    for block_index, block in enumerate(parsed.blocks, start=1):
        content = block.content.strip()
        chunks.append(
            NormalizedChunk(
                stable_ref=_stable_ref(parsed, block_index),
                content_type=block.content_type,
                content=content,
                page_number=block.page_number,
                block_index=block_index,
                bounding_box=block.bounding_box,
                table_metadata={"cells": block.cells} if block.cells is not None else {},
                extraction_method=block.extraction_method,
                extraction_confidence=block.confidence,
                content_hash=sha256(content.encode("utf-8")).hexdigest(),
                token_count=max(1, (len(content) + 3) // 4),
            )
        )
    return chunks


def group_chunks_for_extraction(
    chunks: list[NormalizedChunk], *, max_tokens: int
) -> list[list[NormalizedChunk]]:
    groups: list[list[NormalizedChunk]] = []
    current: list[NormalizedChunk] = []
    current_tokens = 0
    for chunk in chunks:
        if current and current_tokens + chunk.token_count > max_tokens:
            groups.append(current)
            current = []
            current_tokens = 0
        current.append(chunk)
        current_tokens += chunk.token_count
    if current:
        groups.append(current)
    return groups
