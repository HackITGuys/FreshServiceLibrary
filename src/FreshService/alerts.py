"""Alert Management: alerts from monitoring integrations, and their notes.

Needs the Alert Management feature and an agent with the "View Alerts" privilege. The
API cannot create alerts; they only arrive from integrations.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime

from FreshService._util import drop_none, iso
from FreshService.client import FreshServiceClient
from FreshService.models import Alert, AlertNote, AlertSeverity, AlertState, code


def alert_query(
    *,
    severity: Sequence[str | int] = (),
    state: Sequence[str | int] = (),
    since: date | datetime | str | None = None,
    until: date | datetime | str | None = None,
    integration_id: int | None = None,
    workspace_id: int | None = None,
    archived: bool | None = None,
) -> str | None:
    """An alert filter query from common criteria (each list is OR-ed, the criteria AND-ed)."""
    parts: list[str] = []
    if severity:
        parts.append(" OR ".join(f"severity:{code(AlertSeverity, value)}" for value in severity))
    if state:
        parts.append(" OR ".join(f"state:{code(AlertState, value)}" for value in state))
    if since is not None:
        parts.append(f"occurrence_time:>'{iso(since)}'")
    if until is not None:
        parts.append(f"occurrence_time:<'{iso(until)}'")
    if integration_id is not None:
        parts.append(f"integration_id:{integration_id}")
    if workspace_id is not None:
        parts.append(f"workspace_id:{workspace_id}")
    if archived is not None:
        parts.append(f"archived:{str(archived).lower()}")
    if not parts:
        return None
    return " AND ".join(f"({part})" if " OR " in part else part for part in parts)


def list_alerts(
    client: FreshServiceClient,
    query: str | None = None,
    *,
    order_by: str | None = None,
    order_type: str | None = None,
) -> list[Alert]:
    """Alerts, optionally filtered by a query such as alert_query() builds, e.g.
    "(severity:201 OR severity:151) AND state:1". Sort by updated_at, occurrence_time,
    severity, state, id or workspace_id."""
    params = drop_none({"query": query, "order_by": order_by, "order_type": order_type})
    return [Alert.model_validate(raw) for raw in client.paginate("/ams/alerts", "alerts", params)]


def get_alert(client: FreshServiceClient, alert_id: int) -> Alert:
    return Alert.model_validate(client.get_object(f"/ams/alerts/{alert_id}", "alert"))


def list_alert_notes(client: FreshServiceClient, alert_id: int) -> list[AlertNote]:
    return [
        AlertNote.model_validate(raw)
        for raw in client.paginate(f"/ams/alerts/{alert_id}/notes", "alert_notes")
    ]
