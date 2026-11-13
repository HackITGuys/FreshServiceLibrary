from __future__ import annotations

from typing import Any

import responses
from click.testing import CliRunner

from FreshService import FreshServiceClient, apply_plan, plan_software_cleanup
from FreshService.cli import State, main
from tests.conftest import API, asset, get, pages, writes

VENDORS = [
    {"id": 10, "name": "Mozilla"},
    {"id": 11, "name": "OldCorp"},
    {"id": 12, "name": "Fortinet"},  # hardware vendor: never published software
    {"id": 13, "name": "Leftover"},  # no software and nothing referencing it
    {"id": 14, "name": "UsedByAsset"},
    {"id": 15, "name": "Licensor"},
]
SOFTWARE: list[dict[str, Any]] = [
    {"id": 1, "name": "Firefox", "publisher_id": 10, "installation_count": 3, "user_count": 0},
    {"id": 2, "name": "Firefox ESR 60", "publisher_id": 10, "installation_count": 0, "user_count": 0},
    {"id": 3, "name": "OldCorp Tool", "publisher_id": 11, "installation_count": 0, "user_count": 0},
    {"id": 5, "name": "Widget", "publisher_id": 14, "installation_count": 0, "user_count": 0},
    {"id": 6, "name": "Licensed thing", "publisher_id": 15, "installation_count": 0, "user_count": 0},
    {"id": 7, "name": "Mystery", "installation_count": 0, "user_count": 0},  # unregistered
    {
        "id": 8,
        "name": "Ghost",
        "publisher_id": 77,
        "installation_count": 2,
        "user_count": 0,
    },  # deleted publisher
    {"id": 9, "name": "Shared seat", "publisher_id": 15, "installation_count": 0, "user_count": 4},
]


def register(api: responses.RequestsMock) -> None:
    pages(api, "/vendors", "vendors", VENDORS)
    pages(api, "/applications", "applications", SOFTWARE)
    for software_id in (2, 3, 5, 7):
        pages(api, f"/applications/{software_id}/licenses", "licenses", [])
    pages(api, "/applications/6/licenses", "licenses", [{"id": 1, "contract_id": "CNTR-1"}])
    rows = [asset(1, "fw01", 4, vendor=12), asset(2, "kiosk", 2, vendor=14)]
    pages(api, "/assets", "assets", rows, {"include": "type_fields"})
    pages(api, "/contracts", "contracts", [])
    get(api, "/purchase_orders", {"purchase_orders": []})


def test_cleanup_plans_unused_software_then_empty_publishers(
    client: FreshServiceClient, api: responses.RequestsMock
) -> None:
    register(api)
    plan = plan_software_cleanup(client)
    assert writes(api) == []
    assert [(a.action, a.path) for a in plan.actions] == [
        ("delete_software", "/applications/2"),  # Mozilla keeps Firefox, so Mozilla stays
        ("delete_software", "/applications/3"),
        ("delete_software", "/applications/5"),
        ("delete_software", "/applications/7"),  # unregistered last
        ("delete_vendor", "/vendors/11"),
    ]
    assert plan.actions[4].requires == [1]  # only once OldCorp Tool is gone
    assert plan.actions[0].before == {"installations": 0, "users": 0, "licenses": 0, "status": None}
    assert any("'UsedByAsset' has no software left but is kept: asset 2 'kiosk'" in w for w in plan.warnings)
    assert plan.summary["software_to_delete"] == 4
    assert plan.summary["software_kept_in_use"] == 4  # Firefox, Licensed thing, Ghost, Shared seat
    assert plan.summary["publishers_kept_referenced"] == [{"id": 14, "name": "UsedByAsset", "references": 1}]
    # Licenses are only asked for entries with no installations and no users.
    assert not any("/applications/1/licenses" in (c.request.url or "") for c in api.calls)
    assert not any("/applications/9/licenses" in (c.request.url or "") for c in api.calls)


def test_orphan_vendors_and_publisher_scope(client: FreshServiceClient, api: responses.RequestsMock) -> None:
    register(api)
    orphans = plan_software_cleanup(client, orphan_vendors=True)
    vendors = [a.target for a in orphans.actions if a.action == "delete_vendor"]
    assert vendors == [
        "vendor 13 'Leftover' (orphan: no software, no references)",
        "vendor 11 'OldCorp' (no software left)",
    ]
    # Fortinet has no software either, but an asset uses it.
    assert any("'Fortinet'" in w for w in orphans.warnings)

    scoped = plan_software_cleanup(client, publisher="OldCorp")
    assert [(a.action, a.path) for a in scoped.actions] == [
        ("delete_software", "/applications/3"),
        ("delete_vendor", "/vendors/11"),
    ]
    software_only = plan_software_cleanup(client, delete_publishers=False, include_unregistered=False)
    assert [a.path for a in software_only.actions] == [
        "/applications/2",
        "/applications/3",
        "/applications/5",
    ]


def test_vendor_survives_a_failed_software_delete(
    client: FreshServiceClient, api: responses.RequestsMock
) -> None:
    register(api)
    plan = plan_software_cleanup(client, publisher="OldCorp")
    api.delete(API + "/applications/3", status=409, json={"code": "inconsistent_state", "message": "in use"})
    result = apply_plan(client, plan)
    assert [a.status for a in result.actions] == ["failed", "skipped"]
    assert writes(api) == [("DELETE", API + "/applications/3")]


def test_unreadable_references_keep_every_vendor(
    client: FreshServiceClient, api: responses.RequestsMock
) -> None:
    pages(api, "/vendors", "vendors", VENDORS)
    pages(api, "/applications", "applications", SOFTWARE)
    pages(api, "/applications/3/licenses", "licenses", [])
    api.get(API + "/assets", status=403, json={"code": "access_denied", "message": "denied"})
    plan = plan_software_cleanup(client, publisher="OldCorp")
    assert [a.action for a in plan.actions] == ["delete_software"]
    assert any("no vendor is deleted" in w for w in plan.warnings)


def test_cleanup_cli_brief_table(client: FreshServiceClient, api: responses.RequestsMock) -> None:
    register(api)
    result = CliRunner().invoke(main, ["software", "cleanup", "--brief"], obj=State(_client=client))
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0].split() == ["#", "ACTION", "TARGET", "STATUS", "ERROR"]
    assert len(lines) == 6 and lines[5].split()[:3] == ["4", "delete_vendor", "vendor"]
    assert '"software_to_delete": 4' in result.stderr
    assert writes(api) == []
