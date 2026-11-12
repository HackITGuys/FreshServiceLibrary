"""Small helpers shared by the endpoint modules."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from FreshService.client import FreshServiceClient

MAX_QUERY_LENGTH = 512


def drop_none(values: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def iso(value: date | datetime | str | None) -> str | None:
    """A date or datetime as the UTC timestamp the API's *_since parameters take."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if not isinstance(value, datetime):
        value = datetime.combine(value, time.min, tzinfo=UTC)
    elif value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def quoted(query: str) -> str:
    """Filter queries go inside double quotes and may be at most 512 characters long."""
    text = query.strip()
    if not (len(text) >= 2 and text.startswith('"') and text.endswith('"')):
        text = f'"{text}"'
    if len(text) - 2 > MAX_QUERY_LENGTH:
        raise ValueError(f"Filter query is {len(text) - 2} characters; the API accepts at most 512")
    return text


def workspace(client: FreshServiceClient, workspace_id: int | None) -> int | None:
    """An explicit workspace, else FRESH_WORKSPACE_ID, else None (the primary workspace)."""
    return workspace_id if workspace_id is not None else client.settings.workspace_id


def joined(values: Iterable[Any]) -> str | None:
    text = ",".join(str(value) for value in values)
    return text or None
