"""Markdown -> the HTML Freshservice stores in descriptions, notes, articles and announcements.

The API takes HTML. Every library function sends its `body`/`description` as given; call
markdown_to_html() first for Markdown (the CLI does that unless --html is passed).
"""

from __future__ import annotations

import re
from pathlib import Path

import markdown

TABLE_DELIMITER = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")


def _add_missing_table_delimiters(text: str) -> str:
    """Insert the '| --- |' row under table headers that lack one, so they render as tables."""
    lines = text.splitlines()
    fixed = []
    for i, line in enumerate(lines):
        fixed.append(line)
        is_row = line.lstrip().startswith("|")
        starts_table = is_row and (i == 0 or not lines[i - 1].lstrip().startswith("|"))
        next_is_row = i + 1 < len(lines) and lines[i + 1].lstrip().startswith("|")
        if starts_table and next_is_row and not TABLE_DELIMITER.match(lines[i + 1]):
            columns = len(line.strip().strip("|").split("|"))
            fixed.append("|" + " --- |" * columns)
    return "\n".join(fixed)


def markdown_to_html(text: str) -> str:
    """Markdown text to HTML, tables included. HTML passes through unchanged.

    Unlike the pre-1.0 Client.markdown_to_html, a string is never treated as a file path;
    use markdown_file_to_html() for that.
    """
    return markdown.markdown(_add_missing_table_delimiters(text.strip()), extensions=["tables"])


def markdown_file_to_html(path: str | Path) -> str:
    return markdown_to_html(Path(path).read_text(encoding="utf-8"))
