from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from app.modules.document.parsers import ParsedBlock, ParsedDocument


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
    # Section heading and, for table rows, the table's header row. Kept apart
    # from ``content`` so evidence quotes stay verbatim; prepended when the
    # chunk is embedded and returned alongside it in search results.
    context_text: str = ""


def is_context_only(content_type: str, table_metadata: dict[str, Any]) -> bool:
    """Headings and header rows are context for other chunks, not answers."""
    return content_type == "heading" or bool(table_metadata.get("header"))


def embedding_text(content: str, context_text: str) -> str:
    return f"{context_text}\n{content}" if context_text else content


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


def _table_metadata(block: ParsedBlock) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    if block.cells is not None:
        metadata["cells"] = block.cells
    if block.is_table_header:
        metadata["header"] = True
    return metadata


def build_chunks(parsed: ParsedDocument) -> list[NormalizedChunk]:
    chunks: list[NormalizedChunk] = []
    section = ""
    table_headers: dict[int, str] = {}
    for block_index, block in enumerate(parsed.blocks, start=1):
        content = block.content.strip()
        context_parts = [section] if section else []
        if block.content_type == "heading":
            section = content
            table_headers.clear()
            context_parts = []
        elif block.table_index is not None:
            if block.is_table_header:
                table_headers[block.table_index] = content
            elif header := table_headers.get(block.table_index):
                context_parts.append(header)
        context_text = "\n".join(context_parts)
        chunks.append(
            NormalizedChunk(
                stable_ref=_stable_ref(parsed, block_index),
                content_type=block.content_type,
                content=content,
                page_number=block.page_number,
                block_index=block_index,
                bounding_box=block.bounding_box,
                table_metadata=_table_metadata(block),
                extraction_method=block.extraction_method,
                extraction_confidence=block.confidence,
                content_hash=sha256(content.encode("utf-8")).hexdigest(),
                # Context is sent with the chunk to the extraction model too.
                token_count=max(1, (len(content) + len(context_text) + 3) // 4),
                context_text=context_text,
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
