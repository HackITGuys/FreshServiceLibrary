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


def text(client: FreshServiceClient, *args: str) -> str:
    result = CliRunner().invoke(main, list(args), obj=State(_client=client))
    assert result.exit_code == 0, result.output
    return result.stdout


def register_publisher_software(api: responses.RequestsMock) -> None:
    pages(api, "/vendors", "vendors", [{"id": 10, "name": "Mozilla"}, {"id": 11, "name": "Google"}])
    pages(
        api,
        "/applications",
        "applications",
        [
            {"id": 1, "name": "Firefox", "publisher_id": 10, "installation_count": 3, "user_count": 2},
            {"id": 2, "name": "Chrome", "publisher_id": 11, "installation_count": 0},
            {"id": 3, "name": "Mystery", "installation_count": 0},
        ],
    )
    pages(
        api,
        "/applications/1/installations",
        "installations",
        [
            {"installation_machine_id": 1, "version": "127.0"},
            {"installation_machine_id": 2, "version": "128.0"},
            {"installation_machine_id": 3, "version": "128.0"},
        ],
    )


def installation_calls(api: responses.RequestsMock) -> int:
    return sum("/installations" in (call.request.url or "") for call in api.calls)


def test_publishers_list_prints_a_table(client: FreshServiceClient, api: responses.RequestsMock) -> None:
    register_publisher_software(api)
    lines = text(client, "publishers", "list", "--no-unassigned").splitlines()
    assert lines[0].split() == ["ID", "NAME", "SOFTWARE_COUNT", "INSTALLATION_COUNT", "USER_COUNT"]
    assert [line.split() for line in lines[1:]] == [
        ["11", "Google", "1", "0", "0"],  # equal entry counts: by name
        ["10", "Mozilla", "1", "3", "2"],
    ]
    brief = text(client, "publishers", "list", "--search", "moz", "--brief").splitlines()
    assert [line.split() for line in brief] == [
        ["ID", "NAME", "SOFTWARE", "INSTALLS"],
        ["10", "Mozilla", "1", "3"],
    ]
    as_json = run(client, "--json", "publishers", "list")
    assert [row["name"] for row in as_json] == ["Google", "Mozilla", None]  # unassigned last
    assert text(client, "software", "publishers", "--name", "goo").splitlines()[1].split()[:2] == [
        "11",
        "Google",
    ]
    assert installation_calls(api) == 0  # counts come from the API


def test_publisher_software_fetches_usage_unless_brief(
    client: FreshServiceClient, api: responses.RequestsMock
) -> None:
    register_publisher_software(api)
    full = run(client, "publishers", "software", "Mozilla")
    assert [(e["id"], e["publisher_name"], e["device_count"], e["versions"]) for e in full] == [
        (1, "Mozilla", 3, {"128.0": 2, "127.0": 1})
    ]
    calls = installation_calls(api)
    brief = text(client, "publishers", "software", "10", "--brief").splitlines()
    assert [line.split() for line in brief] == [
        ["ID", "NAME", "PUBLISHER", "INSTALLS", "USERS"],
        ["1", "Firefox", "Mozilla", "3", "2"],
    ]
    assert installation_calls(api) == calls  # --brief fetches no installations
    # Chrome reports 0 installations, so its usage is known without a call.
    chrome = run(client, "software", "list", "--publisher", "Google")
    assert chrome[0]["device_count"] == 0 and chrome[0]["versions"] == {}
    assert text(client, "software", "list", "--no-publisher", "--brief").splitlines()[1].split() == [
        "3",
        "Mystery",
        "0",
    ]
    assert "No publisher matches 'Adobe'" in fail(client, "publishers", "software", "Adobe")
    assert "not both" in fail(client, "software", "list", "--publisher", "x", "--no-publisher")


def test_output_file_is_json_even_for_brief(
    client: FreshServiceClient, api: responses.RequestsMock, tmp_path: Path
) -> None:
    register_publisher_software(api)
    target = tmp_path / "publishers.json"
    result = CliRunner().invoke(
        main, ["-o", str(target), "publishers", "list", "--brief"], obj=State(_client=client)
    )
    assert result.exit_code == 0 and result.stdout == ""
    assert json.loads(target.read_text())[1] == {"id": 10, "name": "Mozilla", "software": 1, "installs": 3}


def test_ticket_list_brief(client: FreshServiceClient, api: responses.RequestsMock) -> None:
    pages(
        api,
        "/tickets",
        "tickets",
        [{"id": 7, "subject": "VPN down", "group_id": 3, "status": 2, "description": "long text"}],
    )
    pages(api, "/groups", "groups", [{"id": 3, "name": "Network"}])
    lines = text(client, "tickets", "list", "--brief").splitlines()
    assert lines == ["ID  SUBJECT   GROUP    STATE", " 7  VPN down  Network  open"]
    assert run(client, "--json", "tickets", "list", "--brief") == [
        {"id": 7, "subject": "VPN down", "group": "Network", "state": "open"}
    ]


def test_vendors_get_takes_several_ids(client: FreshServiceClient, api: responses.RequestsMock) -> None:
    from tests.conftest import get

    get(api, "/vendors/11", {"vendor": {"id": 11, "name": "Microsoft"}})
    get(api, "/vendors/12", {"vendor": {"id": 12, "name": "Microsoft Corporation"}})
    api.get(API + "/vendors/99", status=404, json={"code": "not_found", "message": "No vendor"})
    assert run(client, "vendors", "get", "11")["name"] == "Microsoft"  # one id: the object, as before
    several = run(client, "vendors", "get", "11", "12", "99", "11")
    assert [v["id"] for v in several] == [11, 12]  # in order, duplicates once, 99 skipped
    lines = text(client, "vendors", "get", "12", "11", "--brief").splitlines()
    assert [line.split()[0] for line in lines] == ["ID", "12", "11"]
    assert "No vendor with id 99" in fail(client, "vendors", "get", "99")
