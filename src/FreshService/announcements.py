"""Announcements: banners for agents and/or requesters, optionally emailed."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from FreshService._util import drop_none, iso, workspace
from FreshService.client import FreshServiceClient
from FreshService.models import Announcement

STATES = ("active", "archived", "scheduled", "unread")
VISIBILITIES = ("everyone", "agents_only", "agents_and_groups", "requesters_only", "grouped_visibility")


def list_announcements(
    client: FreshServiceClient, *, state: str | None = None, workspace_id: int | None = None
) -> list[Announcement]:
    """Announcements, optionally by state: active, archived, scheduled or unread."""
    if state is not None and state not in STATES:
        raise ValueError(f"state must be one of {', '.join(STATES)}")
    params = drop_none({"state": state, "workspace_id": workspace(client, workspace_id)})
    return [
        Announcement.model_validate(raw) for raw in client.paginate("/announcements", "announcements", params)
    ]


def get_announcement(client: FreshServiceClient, announcement_id: int) -> Announcement:
    return Announcement.model_validate(client.get_object(f"/announcements/{announcement_id}", "announcement"))


def create_announcement(
    client: FreshServiceClient,
    *,
    title: str,
    body_html: str,
    visible_from: datetime | str | None = None,
    visible_till: datetime | str | None = None,
    visibility: str = "everyone",
    departments: Sequence[int] = (),
    groups: Sequence[int] = (),
    send_email: bool | None = None,
    additional_emails: Sequence[str] = (),
    workspace_id: int | None = None,
    **fields: Any,
) -> Announcement:
    """Create an announcement, visible from now unless visible_from says otherwise.
    departments/groups apply with visibility="agents_and_groups"."""
    body = drop_none(
        {
            "title": title,
            "body_html": body_html,
            "visible_from": iso(visible_from or datetime.now(UTC)),
            "visible_till": iso(visible_till),
            "visibility": visibility,
            "departments": list(departments) or None,
            "groups": list(groups) or None,
            "send_email": send_email,
            "additional_emails": list(additional_emails) or None,
            "workspace_id": workspace(client, workspace_id),
            **fields,
        }
    )
    return Announcement.model_validate(client.post("/announcements", body, key="announcement"))


def update_announcement(client: FreshServiceClient, announcement_id: int, **fields: Any) -> Announcement:
    """Change title, body_html, visible_from/visible_till, visibility, departments, groups, ..."""
    body = drop_none(fields)
    for name in ("visible_from", "visible_till"):
        if isinstance(body.get(name), datetime):
            body[name] = iso(body[name])
    return Announcement.model_validate(
        client.put(f"/announcements/{announcement_id}", body, key="announcement")
    )


def delete_announcement(client: FreshServiceClient, announcement_id: int) -> None:
    client.delete(f"/announcements/{announcement_id}")
