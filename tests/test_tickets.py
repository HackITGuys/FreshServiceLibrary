from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import responses

from FreshService import (
    FreshServiceClient,
    FreshServiceSettings,
    add_note,
    create_change,
    create_service_request,
    create_ticket,
    create_ticket_from_template,
    filter_tickets,
    list_changes,
    load_templates,
    markdown_to_html,
    ticket_from_template,
)
from FreshService.changes import add_change_note
from tests.conftest import API, get, pages, query_pages

TEMPLATES = Path(__file__).parent.parent / "examples" / "ticket_templates.json"


def body(api: responses.RequestsMock, index: int = -1) -> dict[str, object]:
    return json.loads(api.calls[index].request.body or b"")  # type: ignore[no-any-return]


def test_template_layering_matches_the_old_generate_ticket() -> None:
    settings = FreshServiceSettings(
        _env_file=None,
        domain="fs.test",
        api_key="k",
        workspace_id=2,
        default_email="soc@example.com",
        default_department_id=7,
        default_group_id=0,
        default_category="General",
        default_subject="Default subject",
    )
    ticket = ticket_from_template(settings, load_templates(TEMPLATES), "CTI", description="<p>x</p>")
    assert ticket["subject"] == "Cyber Threat Intelligence"  # the template's subject wins over the default
    assert ticket["category"] == "CyberSecurity"
    assert ticket["urgency"] == 2 and ticket["source"] == 9
    assert ticket["email"] == "soc@example.com" and ticket["cc_emails"] == ["soc@example.com"]
    assert ticket["department_id"] == 7 and ticket["workspace_id"] == 2
    assert "group_id" not in ticket  # 0 means "not set"
    explicit = ticket_from_template(
        settings, load_templates(TEMPLATES), "CTI", description="x", subject=" Mine "
    )
    assert explicit["subject"] == "Mine"
    with pytest.raises(ValueError, match="No ticket template 'NOPE'"):
        ticket_from_template(settings, {}, "NOPE", description="x")


def test_create_ticket_from_template_posts_the_layered_body(
    client: FreshServiceClient, api: responses.RequestsMock
) -> None:
    api.post(API + "/tickets", json={"ticket": {"id": 9, "subject": "Security Advisory"}})
    templates: dict[str, dict[str, Any]] = {
        "DEFAULT": {"email": "", "priority": 1, "status": 2},
        "ADVISORY": {"subject": "Security Advisory"},
    }
    with pytest.raises(ValueError, match="requester"):
        create_ticket_from_template(client, "ADVISORY", "<p>x</p>", templates=templates)
    client.settings.default_email = "soc@example.com"
    ticket = create_ticket_from_template(client, "ADVISORY", "<p>x</p>", templates=templates)
    assert ticket.id == 9
    assert body(api) == {
        "email": "soc@example.com",
        "priority": 1,
        "status": 2,
        "subject": "Security Advisory",
        "cc_emails": ["soc@example.com"],
        "description": "<p>x</p>",
        "source": 2,
    }


def test_create_ticket_defaults(client: FreshServiceClient, api: responses.RequestsMock) -> None:
    api.post(API + "/tickets", json={"ticket": {"id": 1}})
    create_ticket(client, subject="s", description="<p>d</p>", email="u@example.com", tags=["x"])
    assert body(api) == {
        "subject": "s",
        "description": "<p>d</p>",
        "email": "u@example.com",
        "tags": ["x"],
        "status": 2,
        "priority": 1,
        "source": 2,
    }


def test_add_note_is_private_by_default(client: FreshServiceClient, api: responses.RequestsMock) -> None:
    api.post(
        API + "/tickets/42/notes", json={"conversation": {"id": 5, "private": True, "body": "<p>hi</p>"}}
    )
    note = add_note(client, 42, "<p>hi</p>")
    assert note.id == 5
    assert body(api) == {"body": "<p>hi</p>", "private": True}
    with pytest.raises(ValueError):
        add_note(client, 42, "  ")


def test_filter_tickets_quotes_the_query(client: FreshServiceClient, api: responses.RequestsMock) -> None:
    query_pages(api, "/tickets/filter", "tickets", [{"id": 1}], {"query": '"status:2 AND priority:4"'})
    assert [t.id for t in filter_tickets(client, "status:2 AND priority:4")] == [1]
    with pytest.raises(ValueError, match="512"):
        filter_tickets(client, "x" * 513)


def test_service_request_attachments_become_a_note(
    client: FreshServiceClient, api: responses.RequestsMock, tmp_path: Path
) -> None:
    form = tmp_path / "form.pdf"
    form.write_bytes(b"%PDF")
    api.post(
        API + "/service_catalog/items/12/place_request",
        json={"service_request": {"id": 77, "type": "Service Request"}},
    )
    api.post(API + "/tickets/77/notes", json={"conversation": {"id": 3}})
    request = create_service_request(
        client, 12, requested_for="user@example.com", custom_fields={"cf_laptop": "Mac"}, attachments=[form]
    )
    assert request.id == 77
    assert json.loads(api.calls[0].request.body or b"") == {
        "quantity": 1,
        "requested_for": "user@example.com",
        "custom_fields": {"cf_laptop": "Mac"},
    }
    assert api.calls[1].request.url == API + "/tickets/77/notes"


def test_create_change_needs_a_requester(client: FreshServiceClient, api: responses.RequestsMock) -> None:
    with pytest.raises(ValueError, match="requester"):
        create_change(client, subject="s", description="d")
    api.post(API + "/changes", json={"change": {"id": 3}})
    create_change(
        client, subject="s", description="d", email="me@example.com", planned_start_date="2026-12-01"
    )
    assert body(api)["change_type"] == 1 and body(api)["planned_start_date"] == "2026-12-01"


def test_change_note_attachments_go_on_the_change(
    client: FreshServiceClient, api: responses.RequestsMock, tmp_path: Path
) -> None:
    plan = tmp_path / "rollback.md"
    plan.write_text("steps")
    get(api, "/changes/8", {"change": {"id": 8, "attachments": [{"id": 1, "name": "old.png"}]}})
    api.put(
        API + "/changes/8",
        json={
            "change": {
                "id": 8,
                "attachments": [
                    {"id": 1, "name": "old.png"},
                    {"id": 2, "name": "rollback.md", "attachment_url": "https://files/2"},
                ],
            }
        },
    )
    api.post(API + "/changes/8/notes", json={"note": {"id": 4}})
    add_change_note(client, 8, "<p>Plan</p>", attachments=[plan])
    assert api.calls[1].request.headers["Content-Type"].startswith("multipart/form-data")
    note = body(api)
    assert note["private"] is True
    assert note["body"] == (
        '<p>Plan</p><p>Attached to the change:</p><ul><li><a href="https://files/2">rollback.md</a></li></ul>'
    )


def test_list_changes_takes_query_or_view(client: FreshServiceClient, api: responses.RequestsMock) -> None:
    pages(api, "/changes", "changes", [{"id": 1}], {"query": '"risk:4"'})
    assert [c.id for c in list_changes(client, query="risk:4")] == [1]
    with pytest.raises(ValueError, match="not both"):
        list_changes(client, query="risk:4", view="my_open")


def test_markdown_tables_without_delimiter_rows_render() -> None:
    html = markdown_to_html("| Host | CVE |\n| fw01 | CVE-2026-1 |")
    assert "<table>" in html and "<td>fw01</td>" in html
    already = "<p>Kept <b>as is</b></p>"
    assert markdown_to_html(already) == already
