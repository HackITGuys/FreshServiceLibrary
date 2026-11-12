"""Changes and change notes.

ATTACHMENTS. A change takes attachments on create and update (multipart). Change NOTES
cannot: their `attachments` field is read-only in the API. add_change_note(attachments=...)
therefore uploads the files to the change and links them in the note.
"""

from __future__ import annotations

import html
from collections.abc import Iterable
from datetime import date, datetime
from typing import Any

from FreshService._util import drop_none, iso, quoted, workspace
from FreshService.client import Attachments, FreshServiceClient
from FreshService.models import Change, ChangeStatus, ChangeType, Level, Note, Priority, Risk


def get_change(client: FreshServiceClient, change_id: int) -> Change:
    return Change.model_validate(client.get_object(f"/changes/{change_id}", "change"))


def list_changes(
    client: FreshServiceClient,
    *,
    query: str | None = None,
    view: str | int | None = None,
    updated_since: date | datetime | str | None = None,
    workspace_id: int | None = None,
    order_by: str | None = None,
    order_type: str | None = None,
) -> list[Change]:
    """Changes, optionally filtered by a query ("priority:4 OR risk:4") or a view (name or
    id; not both). workspace_id 0: every workspace."""
    if query and view is not None:
        raise ValueError("The API takes either a query or a view, not both")
    params = drop_none(
        {
            "query": quoted(query) if query else None,
            "view": view,
            "updated_since": iso(updated_since),
            "workspace_id": workspace(client, workspace_id),
            "order_by": order_by,
            "order_type": order_type,
        }
    )
    return [Change.model_validate(raw) for raw in client.paginate("/changes", "changes", params)]


def create_change(
    client: FreshServiceClient,
    *,
    subject: str,
    description: str,
    email: str | None = None,
    requester_id: int | None = None,
    priority: int = Priority.LOW,
    impact: int = Level.LOW,
    risk: int = Risk.LOW,
    change_type: int = ChangeType.MINOR,
    status: int = ChangeStatus.OPEN,
    planned_start_date: datetime | str | None = None,
    planned_end_date: datetime | str | None = None,
    attachments: Attachments = (),
    **fields: Any,
) -> Change:
    """Create a change. Needs a requester (email or requester_id). Other fields (agent_id,
    group_id, department_id, category, custom_fields, planning_fields, assets,
    maintenance_window, ...) go in **fields."""
    if email is None and requester_id is None:
        raise ValueError("A change needs a requester: email or requester_id")
    body = drop_none(
        {
            "subject": subject,
            "description": description,
            "email": email,
            "requester_id": requester_id,
            "priority": priority,
            "impact": impact,
            "risk": risk,
            "change_type": change_type,
            "status": status,
            "planned_start_date": iso(planned_start_date) if planned_start_date else None,
            "planned_end_date": iso(planned_end_date) if planned_end_date else None,
            "workspace_id": client.settings.workspace_id,
            **fields,
        }
    )
    return Change.model_validate(client.post("/changes", body, key="change", attachments=attachments))


def update_change(
    client: FreshServiceClient, change_id: int, *, attachments: Attachments = (), **fields: Any
) -> Change:
    """Change fields of a change and/or add attachments. The workspace can't be changed here."""
    body = drop_none(fields)
    for name in ("planned_start_date", "planned_end_date"):
        if isinstance(body.get(name), date | datetime):
            body[name] = iso(body[name])
    return Change.model_validate(
        client.put(f"/changes/{change_id}", body, key="change", attachments=attachments)
    )


def delete_change(client: FreshServiceClient, change_id: int) -> None:
    client.delete(f"/changes/{change_id}")


def list_change_fields(
    client: FreshServiceClient, *, workspace_id: int | None = None
) -> list[dict[str, Any]]:
    params = drop_none({"workspace_id": workspace(client, workspace_id)})
    return client.get_list("/change_form_fields", "change_fields", params)


# -- notes ---------------------------------------------------------------------------


def list_change_notes(client: FreshServiceClient, change_id: int) -> list[Note]:
    return [Note.model_validate(raw) for raw in client.paginate(f"/changes/{change_id}/notes", "notes")]


def get_change_note(client: FreshServiceClient, change_id: int, note_id: int) -> Note:
    return Note.model_validate(client.get_object(f"/changes/{change_id}/notes/{note_id}", "note"))


def add_change_note(
    client: FreshServiceClient,
    change_id: int,
    body: str,
    *,
    private: bool = True,
    notify_emails: Iterable[str] = (),
    attachments: Attachments = (),
) -> Note:
    """Add a note to a change. Files are attached to the change itself (notes can't hold
    them) and listed with links at the end of the note."""
    if not body.strip():
        raise ValueError("A note needs a body")
    files = list(attachments)
    if files:
        before = {attachment.id for attachment in get_change(client, change_id).attachments}
        change = update_change(client, change_id, attachments=files)
        added = [attachment for attachment in change.attachments if attachment.id not in before]
        links = "".join(
            f'<li><a href="{html.escape(item.attachment_url or "")}">'
            f"{html.escape(item.name or 'attachment')}</a></li>"
            for item in added
        )
        body += f"<p>Attached to the change:</p><ul>{links}</ul>" if links else ""
    payload = drop_none({"body": body, "private": private, "notify_emails": list(notify_emails) or None})
    return Note.model_validate(client.post(f"/changes/{change_id}/notes", payload, key="note"))


def update_change_note(client: FreshServiceClient, change_id: int, note_id: int, body: str) -> Note:
    """Replace a note's body (its visibility can't be changed)."""
    return Note.model_validate(
        client.put(f"/changes/{change_id}/notes/{note_id}", {"body": body}, key="note")
    )


def delete_change_note(client: FreshServiceClient, change_id: int, note_id: int) -> None:
    client.delete(f"/changes/{change_id}/notes/{note_id}")
