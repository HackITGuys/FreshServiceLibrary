"""Tickets (incidents and service requests) and their conversations: replies and notes.

Bodies and descriptions are HTML, sent as given; use markdown_to_html() for Markdown.
Attachments are local file paths; with any, the request goes as multipart/form-data.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
from typing import Any

from FreshService._util import drop_none, iso, joined, quoted, workspace
from FreshService.client import Attachments, FreshServiceClient
from FreshService.config import FreshServiceSettings
from FreshService.models import Conversation, Priority, Ticket, TicketSource, TicketStatus

DEFAULT_TEMPLATE = "DEFAULT"

# -- tickets -----------------------------------------------------------------------


def get_ticket(client: FreshServiceClient, ticket_id: int, include: Sequence[str] = ()) -> Ticket:
    """One ticket. include: conversations, requester, stats, assets, requested_for, ... (each
    embed costs 2 extra API credits)."""
    params = drop_none({"include": joined(include)})
    return Ticket.model_validate(client.get_object(f"/tickets/{ticket_id}", "ticket", params))


def list_tickets(
    client: FreshServiceClient,
    *,
    predefined: str | None = None,
    requester_id: int | None = None,
    email: str | None = None,
    updated_since: date | datetime | str | None = None,
    type: str | None = None,
    include: Sequence[str] = (),
    workspace_id: int | None = None,
    order_type: str | None = None,
) -> list[Ticket]:
    """Tickets, newest first. Without updated_since the API returns only tickets created in
    the last 30 days. predefined: new_and_my_open, watching, spam or deleted."""
    params = drop_none(
        {
            "filter": predefined,
            "requester_id": requester_id,
            "email": email,
            "updated_since": iso(updated_since),
            "type": type,
            "include": joined(include),
            "workspace_id": workspace(client, workspace_id),
            "order_type": order_type,
        }
    )
    return [Ticket.model_validate(raw) for raw in client.paginate("/tickets", "tickets", params)]


def filter_tickets(
    client: FreshServiceClient, query: str, *, workspace_id: int | None = None
) -> list[Ticket]:
    """Tickets matching a filter query, e.g. "status:2 AND priority:>3 AND tag:'vpn'".

    Fields are snake_case names from list_ticket_fields(); strings in single quotes,
    dates as 'YYYY-MM-DD', :> and :< for ranges, null for unset. At most 512 characters.
    """
    params = drop_none({"query": quoted(query), "workspace_id": workspace(client, workspace_id)})
    return [Ticket.model_validate(raw) for raw in client.paginate_query("/tickets/filter", "tickets", params)]


def create_ticket(
    client: FreshServiceClient,
    *,
    subject: str,
    description: str,
    email: str | None = None,
    requester_id: int | None = None,
    attachments: Attachments = (),
    **fields: Any,
) -> Ticket:
    """Create a ticket. Needs a requester (email or requester_id). Any other ticket field
    (priority, status, urgency, impact, group_id, category, tags, custom_fields, ...)
    goes in **fields; status defaults to Open, priority to Low and source to Portal."""
    if not description.strip():
        raise ValueError("A ticket needs a description")
    body = drop_none(
        {
            "subject": subject,
            "description": description,
            "email": email,
            "requester_id": requester_id,
            "workspace_id": client.settings.workspace_id,
            **fields,
        }
    )
    body.setdefault("status", TicketStatus.OPEN)
    body.setdefault("priority", Priority.LOW)
    body.setdefault("source", TicketSource.PORTAL)
    if "email" not in body and "requester_id" not in body and "phone" not in body:
        raise ValueError("A ticket needs a requester: email, requester_id or phone")
    return Ticket.model_validate(client.post("/tickets", body, key="ticket", attachments=attachments))


def update_ticket(
    client: FreshServiceClient, ticket_id: int, *, attachments: Attachments = (), **fields: Any
) -> Ticket:
    """Change ticket fields. `tags` and `assets` replace the existing lists; attachments are added."""
    body = drop_none(fields)
    return Ticket.model_validate(
        client.put(f"/tickets/{ticket_id}", body, key="ticket", attachments=attachments)
    )


def delete_ticket(client: FreshServiceClient, ticket_id: int) -> None:
    """Move a ticket to trash (restore_ticket() brings it back)."""
    client.delete(f"/tickets/{ticket_id}")


def restore_ticket(client: FreshServiceClient, ticket_id: int) -> None:
    client.request("PUT", f"/tickets/{ticket_id}/restore")


def list_ticket_fields(
    client: FreshServiceClient, *, workspace_id: int | None = None
) -> list[dict[str, Any]]:
    """The ticket form's fields, with the names filter queries use and dropdown choices."""
    params = drop_none({"workspace_id": workspace(client, workspace_id)})
    return client.get_list("/ticket_form_fields", "ticket_fields", params)


# -- templates (the pre-1.0 generate_ticket behaviour) ------------------------------


def load_templates(path: str | Path) -> dict[str, dict[str, Any]]:
    """A JSON file of named ticket bodies; "DEFAULT" is applied under every other one."""
    with Path(path).open(encoding="utf-8") as handle:
        templates = json.load(handle)
    if not isinstance(templates, dict) or not all(isinstance(value, dict) for value in templates.values()):
        raise ValueError(f"{path}: expected an object of named template objects")
    return templates


def ticket_from_template(
    settings: FreshServiceSettings,
    templates: Mapping[str, Mapping[str, Any]],
    name: str,
    *,
    description: str,
    subject: str | None = None,
) -> dict[str, Any]:
    """A ticket body: DEFAULT template < FRESH_DEFAULT_* settings < the named template <
    an explicit subject. Empty placeholders ("" or 0) are dropped."""
    if name not in templates:
        raise ValueError(f"No ticket template {name!r}; defined: {', '.join(templates) or 'none'}")
    body: dict[str, Any] = deepcopy(dict(templates.get(DEFAULT_TEMPLATE, {})))
    defaults = {
        "workspace_id": settings.workspace_id,
        "email": settings.default_email,
        "department_id": settings.default_department_id,
        "group_id": settings.default_group_id,
        "category": settings.default_category,
        "subject": settings.default_subject,
    }
    body.update(drop_none(defaults))
    if settings.default_email:
        cc = list(body.get("cc_emails") or [])
        if settings.default_email not in cc:
            cc.append(settings.default_email)
        body["cc_emails"] = cc
    if name != DEFAULT_TEMPLATE:
        body.update(deepcopy(dict(templates[name])))
    if subject and subject.strip():
        body["subject"] = subject.strip()
    body["description"] = description
    return {
        key: value
        for key, value in body.items()
        if value not in ("", None) and not (value == 0 and key.endswith("_id"))
    }


def create_ticket_from_template(
    client: FreshServiceClient,
    name: str,
    description: str,
    *,
    subject: str | None = None,
    templates: Mapping[str, Mapping[str, Any]] | None = None,
    attachments: Attachments = (),
) -> Ticket:
    """create_ticket() with the body built by ticket_from_template() (FRESH_TEMPLATE_FILE)."""
    if templates is None:
        if client.settings.template_file is None:
            raise ValueError("No templates given and FRESH_TEMPLATE_FILE is not set")
        templates = load_templates(client.settings.template_file)
    body = ticket_from_template(client.settings, templates, name, description=description, subject=subject)
    return create_ticket(client, attachments=attachments, **body)


# -- conversations ------------------------------------------------------------------


def list_conversations(client: FreshServiceClient, ticket_id: int) -> list[Conversation]:
    """Every reply and note on a ticket, oldest first."""
    return [
        Conversation.model_validate(raw)
        for raw in client.paginate(f"/tickets/{ticket_id}/conversations", "conversations")
    ]


def add_note(
    client: FreshServiceClient,
    ticket_id: int,
    body: str,
    *,
    private: bool = True,
    notify_emails: Iterable[str] = (),
    attachments: Attachments = (),
    user_id: int | None = None,
    incoming: bool | None = None,
) -> Conversation:
    """Add a note (private unless private=False) to a ticket or service request."""
    if not body.strip():
        raise ValueError("A note needs a body")
    payload = drop_none(
        {
            "body": body,
            "private": private,
            "notify_emails": list(notify_emails) or None,
            "user_id": user_id,
            "incoming": incoming,
        }
    )
    return Conversation.model_validate(
        client.post(f"/tickets/{ticket_id}/notes", payload, key="conversation", attachments=attachments)
    )


def reply(
    client: FreshServiceClient,
    ticket_id: int,
    body: str,
    *,
    cc_emails: Iterable[str] = (),
    bcc_emails: Iterable[str] = (),
    from_email: str | None = None,
    user_id: int | None = None,
    attachments: Attachments = (),
) -> Conversation:
    """Reply to the requester (an email goes out)."""
    if not body.strip():
        raise ValueError("A reply needs a body")
    payload = drop_none(
        {
            "body": body,
            "cc_emails": list(cc_emails) or None,
            "bcc_emails": list(bcc_emails) or None,
            "from_email": from_email,
            "user_id": user_id,
        }
    )
    return Conversation.model_validate(
        client.post(f"/tickets/{ticket_id}/reply", payload, key="conversation", attachments=attachments)
    )


def update_conversation(
    client: FreshServiceClient, conversation_id: int, body: str, *, attachments: Attachments = ()
) -> Conversation:
    """Replace a note's body (only notes can be edited), optionally adding attachments."""
    return Conversation.model_validate(
        client.put(
            f"/conversations/{conversation_id}", {"body": body}, key="conversation", attachments=attachments
        )
    )


def delete_conversation(client: FreshServiceClient, conversation_id: int) -> None:
    client.delete(f"/conversations/{conversation_id}")


def delete_conversation_attachment(
    client: FreshServiceClient, conversation_id: int, attachment_id: int
) -> None:
    client.delete(f"/conversations/{conversation_id}/attachments/{attachment_id}")


def delete_ticket_attachment(client: FreshServiceClient, ticket_id: int, attachment_id: int) -> None:
    client.delete(f"/tickets/{ticket_id}/attachments/{attachment_id}")
