"""Split extracted PDF page text into retrieval-sized blocks.

pypdf returns a page as visual lines: sentences are hard-wrapped, and
register/alarm tables come out as one row per line. Treating the page as a
single block dilutes every table row and paragraph into one vector, so this
module rebuilds the page's structure from layout cues that hold across
manuals:

- headings: numbered chapter/section titles on a line of their own;
- table rows: lines that start with an identifier (alarm code, register
  address, parameter number) followed by text, grouped into tables, with the
  label line directly above the first row kept as the table's header row;
- prose: wrapped lines rejoined into sentences, then short consecutive
  paragraphs merged up to ``target_chars`` so a block carries enough context.

The text of every block is the page text verbatim apart from rejoining
wrapped lines, so evidence quotes stay faithful to the manual.
"""

import re
from dataclasses import dataclass

_TERMINATORS = tuple("。！？；：.!?;:")
# Unambiguous chapter markers; may follow a short prefix such as a model name.
_STRONG_HEADING = re.compile(
    r"第\s*[0-9一二三四五六七八九十百]+\s*[章节篇]|^(?:chapter|section)\s+\d+",
    re.IGNORECASE,
)
# "3 安装" / "5.2 通讯设置": only a heading when not continuing a wrapped line.
_NUMBERED_HEADING = re.compile(r"^\d{1,2}(?:\.\d{1,2}){0,3}\s+[^\d\s]")
_ROW = re.compile(r"^(?:[A-Za-z]{1,3}\d[\w.\-]*|\d{3,6})\s+\S")
_LIST_ITEM = re.compile(r"^(?:\d{1,2}[.、)）]|[(（]\d{1,2}[)）]|[-•·])\s*\S")
_MAX_HEADING_CHARS = 40


@dataclass(frozen=True)
class Segment:
    kind: str  # "heading" | "text" | "table_header" | "table_row"
    content: str


def _heading_strength(line: str) -> int:
    """2 = certain heading, 1 = heading unless it continues a wrapped line."""
    if (
        len(line) > _MAX_HEADING_CHARS
        or line.endswith(_TERMINATORS)
        or any(mark in line for mark in "，。；")
    ):
        return 0
    if _STRONG_HEADING.search(line):
        return 2
    if _NUMBERED_HEADING.match(line):
        return 1
    return 0


def _is_row(line: str) -> bool:
    return _ROW.match(line) is not None


def _looks_like_header(line: str) -> bool:
    return (
        len(line.split()) >= 3
        and not any(character.isdigit() for character in line)
        and not line.endswith(_TERMINATORS)
    )


def _join(left: str, right: str) -> str:
    if (
        left
        and right
        and left[-1].isascii()
        and right[0].isascii()
        and left[-1].isalnum()
        and right[0].isalnum()
    ):
        return f"{left} {right}"
    return left + right


def _lines_to_segments(lines: list[str]) -> list[Segment]:
    segments: list[Segment] = []
    for line in lines:
        previous = segments[-1] if segments else None
        open_text = (
            previous is not None
            and previous.kind == "text"
            and not previous.content.endswith(_TERMINATORS)
            and not _looks_like_header(previous.content)
        )
        continues = (
            previous is not None
            and previous.kind in {"text", "table_row"}
            and not previous.content.endswith(_TERMINATORS)
            and not (previous.kind == "text" and _looks_like_header(previous.content))
        )
        if _is_row(line) and not open_text:
            segments.append(Segment("table_row", line))
            continue
        strength = _heading_strength(line)
        if strength == 2 or (strength == 1 and not continues):
            segments.append(Segment("heading", line))
            continue
        if continues and not (previous.kind == "table_row" and _LIST_ITEM.match(line)):
            segments[-1] = Segment(previous.kind, _join(previous.content, line))
            continue
        segments.append(Segment("text", line))

    # The unterminated label line right above a table is its header row.
    for index in range(1, len(segments)):
        current, previous = segments[index], segments[index - 1]
        if (
            current.kind == "table_row"
            and previous.kind == "text"
            and _looks_like_header(previous.content)
        ):
            segments[index - 1] = Segment("table_header", previous.content)
    return segments


def _merge_prose(
    segments: list[Segment], *, target_chars: int, max_chars: int
) -> list[Segment]:
    merged: list[Segment] = []
    for segment in segments:
        previous = merged[-1] if merged else None
        if segment.kind == "text" and previous and previous.kind == "text":
            combined = len(previous.content) + len(segment.content)
            keeps_list_together = (
                previous.content.endswith(("：", ":"))
                or _LIST_ITEM.match(segment.content) is not None
            )
            if combined <= target_chars or (
                keeps_list_together and combined <= max_chars
            ):
                merged[-1] = Segment("text", f"{previous.content}\n{segment.content}")
                continue
        merged.append(segment)
    return merged


def segment_page_text(
    text: str, *, target_chars: int = 300, max_chars: int = 600
) -> list[Segment]:
    lines = [line.strip() for line in text.splitlines()]
    segments = _lines_to_segments([line for line in lines if line])
    return _merge_prose(segments, target_chars=target_chars, max_chars=max_chars)
