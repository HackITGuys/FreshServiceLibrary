from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import responses
from click.testing import CliRunner

from FreshService import FreshServiceClient
from FreshService.cli import State, main, parse_fields
from tests.conftest import API, asset, pages, register_asset_types, writes


def run(client: FreshServiceClient, *args: str, input: str | None = None) -> Any:
    result = CliRunner().invoke(main, list(args), obj=State(_client=client), input=input)
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def fail(client: FreshServiceClient, *args: str) -> str:
    result = CliRunner().invoke(main, list(args), obj=State(_client=client))
    assert result.exit_code != 0
    return result.output


def test_parse_fields() -> None:
    assert parse_fields(["priority=3", "custom_fields.cf_team=Ops", 'tags=["a"]', "name=x=y"]) == {
        "priority": 3,
        "custom_fields": {"cf_team": "Ops"},
        "tags": ["a"],
        "name": "x=y",
    }


def test_note_converts_markdown(
    client: FreshServiceClient, api: responses.RequestsMock, tmp_path: Path
) -> None:
    api.post(API + "/tickets/12/notes", json={"conversation": {"id": 1, "private": True}})
    out = run(client, "tickets", "note", "12", "--body", "**Patched**", "--notify", "a@b.c")
    assert out["id"] == 1
    assert json.loads(api.calls[0].request.body or b"") == {
        "body": "<p><strong>Patched</strong></p>",
        "private": True,
        "notify_emails": ["a@b.c"],
    }


def test_ticket_create_names_and_fields(client: FreshServiceClient, api: responses.RequestsMock) -> None:
    api.post(API + "/tickets", json={"ticket": {"id": 5}})
    run(
        client,
        "tickets",
        "create",
        "--subject",
        "VPN down",
        "--body",
        "<p>x</p>",
        "--html",
        "--email",
        "u@example.com",
        "--priority",
        "urgent",
        "--field",
        "custom_fields.cf_site=Oslo",
    )
    sent = json.loads(api.calls[0].request.body or b"")
    assert (
        sent["priority"] == 4
        and sent["custom_fields"] == {"cf_site": "Oslo"}
        and sent["description"] == "<p>x</p>"
    )
    assert "is not one of" in fail(
        client, "tickets", "create", "--subject", "s", "--body", "b", "--priority", "huge"
    )


def test_delete_asks_unless_yes(client: FreshServiceClient, api: responses.RequestsMock) -> None:
    api.delete(API + "/announcements/3")
    result = CliRunner().invoke(
        main, ["announcements", "delete", "3"], obj=State(_client=client), input="n\n"
    )
    assert result.exit_code == 1 and writes(api) == []
    assert run(client, "announcements", "delete", "3", "--yes") == {"deleted": 3}


def test_bulk_commands_plan_unless_apply(client: FreshServiceClient, api: responses.RequestsMock) -> None:
    register_asset_types(api, 4)
    pages(api, "/assets", "assets", [asset(1, "fw01", 4)], {"include": "type_fields"})
    pages(api, "/vendors", "vendors", [{"id": 99, "name": "Unknown"}])
    pages(api, "/products", "products", [{"id": 60, "name": "Unknown (Firewall)", "asset_type_id": 4}])
    plan = run(client, "assets", "tag-unknown", "--type", "Firewall")
    assert plan["applied"] is False and len(plan["actions"]) == 1 and writes(api) == []

    api.put(API + "/assets/1", json={"asset": {"id": 1001, "display_id": 1}})
    done = run(client, "assets", "tag-unknown", "--type", "Firewall", "--apply")
    assert done["applied"] is True and done["actions"][0]["status"] == "done"
    assert writes(api) == [("PUT", API + "/assets/1")]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["articles", "list"], "exactly one of"),
        (["software", "where"], "--publisher and/or --name"),
        (["tickets", "update", "1"], "Nothing to change"),
    ],
)
def test_usage_errors(client: FreshServiceClient, args: list[str], message: str) -> None:
    assert message in fail(client, *args)


def test_api_errors_are_one_line(client: FreshServiceClient, api: responses.RequestsMock) -> None:
    api.get(API + "/tickets/404", status=404, json={"code": "not_found", "message": "No ticket"})
    assert "Error: GET /api/v2/tickets/404 returned HTTP 404: No ticket" in fail(
        client, "tickets", "get", "404"
    )


def test_software_publishers_and_list_by_publisher(
    client: FreshServiceClient, api: responses.RequestsMock
) -> None:
    pages(api, "/vendors", "vendors", [{"id": 10, "name": "Mozilla"}, {"id": 11, "name": "Google"}])
    pages(
        api,
        "/applications",
        "applications",
        [
            {"id": 1, "name": "Firefox", "publisher_id": 10, "installation_count": 5},
            {"id": 2, "name": "Chrome", "publisher_id": 11},
            {"id": 3, "name": "Mystery"},
        ],
    )
    publishers = run(client, "software", "publishers", "--no-unassigned")
    assert [(p["name"], p["software_count"], p["installation_count"]) for p in publishers] == [
        ("Google", 1, 0),
        ("Mozilla", 1, 5),
    ]
    by_publisher = run(client, "software", "list", "--publisher", "Mozilla")
    assert [(s["id"], s["publisher_name"]) for s in by_publisher] == [(1, "Mozilla")]
    assert [s["id"] for s in run(client, "software", "list", "--vendor", "11")] == [2]
    assert [s["id"] for s in run(client, "software", "list", "--no-publisher")] == [3]
    assert "not both" in fail(client, "software", "list", "--publisher", "x", "--no-publisher")
