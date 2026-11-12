from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

import pytest
import responses

from FreshService import (
    FreshServiceClient,
    apply_plan,
    plan_set_product_vendor,
    plan_software_delete,
    plan_software_merge,
    plan_tag_unknown,
    plan_vendor_merge,
)
from tests.conftest import API, asset, get, pages, register_asset_types, writes


def sent(api: responses.RequestsMock) -> list[tuple[str, str, Any]]:
    out = []
    for call in api.calls:
        if call.request.method == "GET":
            continue
        raw = call.request.body
        out.append(
            (
                call.request.method or "",
                (call.request.url or "").removeprefix(API),
                json.loads(raw) if raw else None,
            )
        )
    return out


# -- tag-unknown -------------------------------------------------------------------------


def register_network(
    api: responses.RequestsMock, vendors: list[dict[str, Any]], products: list[dict[str, Any]]
) -> None:
    register_asset_types(api, 4, 5)
    rows = [
        asset(1, "fw01", 4),
        asset(2, "fw02", 4, product=50, vendor=10),
        asset(3, "sw01", 5, vendor=10),
        asset(4, "pc01", 2),
    ]
    pages(api, "/assets", "assets", rows, {"include": "type_fields"})
    pages(api, "/vendors", "vendors", vendors)
    pages(api, "/products", "products", products)


def test_tag_unknown_plans_without_writing(client: FreshServiceClient, api: responses.RequestsMock) -> None:
    register_network(
        api, [{"id": 10, "name": "Fortinet"}], [{"id": 50, "name": "FortiGate 100F", "asset_type_id": 4}]
    )
    plan = plan_tag_unknown(client, ["Firewall", "Switch"])
    assert writes(api) == []
    assert [(a.action, a.target) for a in plan.actions] == [
        ("create_product", "product 'Unknown (Firewall)' for asset type Hardware > Network > Firewall"),
        ("create_vendor", "vendor 'Unknown'"),
        ("update_asset", "asset 1 'fw01' (Firewall)"),
        ("create_product", "product 'Unknown (Switch)' for asset type Hardware > Network > Switch"),
        ("update_asset", "asset 3 'sw01' (Switch)"),
    ]
    fw01 = plan.actions[2]
    assert fw01.body == {"type_fields": {"product_1": "<ref:product:4>", "vendor_1": "<ref:vendor:Unknown>"}}
    assert plan.actions[4].body == {"type_fields": {"product_1": "<ref:product:5>"}}  # sw01 keeps its vendor
    assert plan.summary["assets_scanned"] == 3 and plan.summary["assets_to_update"] == 2


def test_tag_unknown_reuses_existing_placeholders_and_applies(
    client: FreshServiceClient, api: responses.RequestsMock
) -> None:
    register_network(
        api,
        [{"id": 10, "name": "Fortinet"}, {"id": 99, "name": "unknown"}],
        [{"id": 60, "name": "Unknown (Firewall)", "asset_type_id": 4}],
    )
    plan = plan_tag_unknown(client, ["Network"])
    assert [a.action for a in plan.actions] == ["update_asset", "create_product", "update_asset"]
    api.post(API + "/products", json={"product": {"id": 61, "name": "Unknown (Switch)"}})
    api.put(API + "/assets/1", json={"asset": {"id": 1001, "display_id": 1}})
    api.put(API + "/assets/3", json={"asset": {"id": 1003, "display_id": 3}})
    result = apply_plan(client, plan)
    assert result.applied and result.summary["done"] == 3
    assert sent(api) == [
        ("PUT", "/assets/1", {"type_fields": {"product_1": 60, "vendor_1": 99}}),
        ("POST", "/products", {"name": "Unknown (Switch)", "asset_type_id": 5, "manufacturer": "Unknown"}),
        ("PUT", "/assets/3", {"type_fields": {"product_1": 61}}),
    ]
    assert plan.actions[0].status == "planned"  # apply_plan works on a copy


def test_failed_create_skips_its_dependants(client: FreshServiceClient, api: responses.RequestsMock) -> None:
    register_network(
        api, [{"id": 10, "name": "Fortinet"}], [{"id": 50, "name": "FortiGate", "asset_type_id": 4}]
    )
    plan = plan_tag_unknown(client, ["Firewall"], fill=["vendor"])
    api.post(API + "/vendors", status=409, json={"code": "duplicate_value", "message": "exists"})
    result = apply_plan(client, plan)
    assert [a.status for a in result.actions] == ["failed", "skipped"]
    assert "vendor:Unknown" in (result.actions[1].error or "")
    assert [m for m, _, _ in sent(api)] == ["POST"]


def test_set_product_vendor_on_placeholder_assets(
    client: FreshServiceClient, api: responses.RequestsMock
) -> None:
    register_network(
        api,
        [{"id": 10, "name": "Fortinet"}, {"id": 99, "name": "Unknown"}],
        [
            {"id": 60, "name": "Unknown (Firewall)", "asset_type_id": 4},
            {"id": 50, "name": "FortiGate 100F", "asset_type_id": 4},
        ],
    )
    with pytest.raises(ValueError, match="Select assets"):
        plan_set_product_vendor(client, vendor="Fortinet")
    with pytest.raises(ValueError, match="No vendor named 'Palo Alto'"):
        plan_set_product_vendor(client, vendor="Palo Alto", types=["Firewall"])
    plan = plan_set_product_vendor(
        client, product="FortiGate 100F", vendor="Fortinet", types=["Firewall"], name="fw*"
    )
    # fw02 already has both; fw01 gets both.
    assert [(a.target, a.body) for a in plan.actions] == [
        ("asset 1 'fw01' (Firewall)", {"type_fields": {"product_1": 50, "vendor_1": 10}})
    ]
    created = plan_set_product_vendor(
        client, product="FortiSwitch 248E", vendor="Fortinet", types=["Switch"], create_missing=True
    )
    assert [a.action for a in created.actions] == ["create_product", "update_asset"]
    assert created.actions[0].body == {
        "name": "FortiSwitch 248E",
        "asset_type_id": 5,
        "manufacturer": "Fortinet",
    }


# -- vendor merge --------------------------------------------------------------------------


def test_vendor_merge(client: FreshServiceClient, api: responses.RequestsMock) -> None:
    pages(
        api,
        "/vendors",
        "vendors",
        [
            {"id": 1, "name": "Microsoft"},
            {"id": 2, "name": "Microsoft Corporation"},
            {"id": 3, "name": "Microsoft Corp."},
        ],
    )
    pages(
        api,
        "/applications",
        "applications",
        [{"id": 100, "name": "Teams", "publisher_id": 2}, {"id": 101, "name": "Edge", "publisher_id": 1}],
    )
    register_asset_types(api)
    pages(
        api,
        "/assets",
        "assets",
        [asset(1, "pc01", 2, vendor=3), asset(2, "pc02", 2, vendor=1)],
        {"include": "type_fields"},
    )
    pages(api, "/contracts", "contracts", [{"id": 7, "name": "EA", "vendor_id": 2}])
    get(api, "/purchase_orders", {"purchase_orders": [{"id": 4, "po_number": "PO-4", "vendor_id": 3}]})

    plan = plan_vendor_merge(client, ["Microsoft Corp*"], "Microsoft", delete=True)
    assert writes(api) == []
    assert [(a.action, a.path, a.body) for a in plan.actions] == [
        ("update_software", "/applications/100", {"publisher_id": 1}),
        ("update_asset", "/assets/1", {"type_fields": {"vendor_1": 1}}),
        ("update_contract", "/contracts/7", {"vendor_id": 1}),
        ("delete_vendor", "/vendors/2", None),
        ("delete_vendor", "/vendors/3", None),
    ]
    assert plan.actions[3].requires == [0, 2] and plan.actions[4].requires == [1]
    assert any("PO-4" in warning for warning in plan.warnings)
    with pytest.raises(ValueError, match="is a pattern"):
        plan_vendor_merge(client, ["Microsoft"], "Microsoft*")


# -- software merge ------------------------------------------------------------------------


def register_chrome(api: responses.RequestsMock, *, target: bool) -> None:
    pages(api, "/vendors", "vendors", [{"id": 11, "name": "Google"}, {"id": 12, "name": "Other"}])
    entries = [
        {"id": 200, "name": "Google Chrome 120", "publisher_id": 11, "application_type": "desktop"},
        {"id": 201, "name": "Google Chrome 121", "publisher_id": 11, "application_type": "desktop"},
        {"id": 202, "name": "Google Chrome 122 (licensed)", "publisher_id": 11},
        {"id": 300, "name": "Google Chrome 121", "publisher_id": 12},
    ]
    if target:
        entries.append({"id": 210, "name": "Google Chrome", "publisher_id": 11})
    pages(api, "/applications", "applications", entries)
    pages(
        api,
        "/applications/200/installations",
        "installations",
        [
            {"installation_machine_id": 1, "version": "120.0", "installation_path": "C:/c"},
            {"installation_machine_id": 2, "version": "120.0"},
        ],
    )
    pages(
        api,
        "/applications/201/installations",
        "installations",
        [
            {"installation_machine_id": 2, "version": "121.0"},
            {"installation_machine_id": 3, "version": "121.0"},
        ],
    )
    pages(
        api,
        "/applications/202/installations",
        "installations",
        [{"installation_machine_id": 9, "version": "122"}],
    )
    pages(api, "/applications/200/users", "application_users", [{"user_id": 5}])
    pages(api, "/applications/201/users", "application_users", [{"user_id": 5}, {"user_id": 6}])
    pages(api, "/applications/202/users", "application_users", [])
    pages(api, "/applications/200/licenses", "licenses", [])
    pages(api, "/applications/201/licenses", "licenses", [])
    pages(api, "/applications/202/licenses", "licenses", [{"id": 1, "contract_id": "CNTR-1"}])
    if target:
        pages(
            api,
            "/applications/210/installations",
            "installations",
            [{"installation_machine_id": 3, "version": "122"}],
        )
        pages(api, "/applications/210/users", "application_users", [])


def test_software_merge_into_an_existing_entry(
    client: FreshServiceClient, api: responses.RequestsMock
) -> None:
    register_chrome(api, target=True)
    with pytest.raises(ValueError, match="merges stay within one vendor"):
        plan_software_merge(client, "Google", ["300"], "Google Chrome")
    plan = plan_software_merge(client, "Google", ["Google Chrome 1*"], "Google Chrome")
    assert writes(api) == []
    assert any("license" in warning for warning in plan.warnings)  # 202 is left alone
    rows = [(a.action, a.method, a.path, a.params or a.body) for a in plan.actions]
    assert rows == [
        # device 1 from 120; device 2 is on both sources, the higher version (121) wins;
        # device 3 is already on the target.
        (
            "add_installation",
            "POST",
            "/applications/210/installations",
            {"installation_machine_id": 1, "installation_path": "C:/c", "version": "120.0"},
        ),
        (
            "add_installation",
            "POST",
            "/applications/210/installations",
            {"installation_machine_id": 2, "version": "121.0"},
        ),
        ("remove_installations", "DELETE", "/applications/200/installations", {"device_ids": "1,2"}),
        ("add_software_users", "POST", "/applications/210/users", {"application_users": [{"user_id": 5}]}),
        ("remove_software_users", "DELETE", "/applications/200/users", {"user_ids": "5"}),
        ("ignore_software", "PUT", "/applications/200", {"status": "ignored"}),
        ("remove_installations", "DELETE", "/applications/201/installations", {"device_ids": "2,3"}),
        ("add_software_users", "POST", "/applications/210/users", {"application_users": [{"user_id": 6}]}),
        ("remove_software_users", "DELETE", "/applications/201/users", {"user_ids": "5,6"}),
        ("ignore_software", "PUT", "/applications/201", {"status": "ignored"}),
    ]
    assert plan.actions[2].requires == [0, 1]
    assert plan.actions[5].requires == [2, 4]
    assert plan.summary["sources_become"] == "ignored"


def test_software_merge_creates_the_target_and_deletes_on_request(
    client: FreshServiceClient, api: responses.RequestsMock
) -> None:
    register_chrome(api, target=False)
    plan = plan_software_merge(client, "Google", ["200", "201"], "Google Chrome", delete=True)
    assert plan.actions[0].action == "create_software"
    assert plan.actions[0].body == {
        "name": "Google Chrome",
        "publisher_id": 11,
        "application_type": "desktop",
    }
    assert plan.actions[1].path == "/applications/<ref:software:target>/installations"
    assert [a.action for a in plan.actions][-1] == "delete_software"

    # Applying: the target is created, then one installation POST fails -> no source is emptied.
    api.post(API + "/applications", json={"application": {"id": 500, "name": "Google Chrome"}})
    api.post(API + "/applications/500/installations", json={"installation": {"installation_machine_id": 1}})
    api.post(API + "/applications/500/installations", status=400, json={"description": "bad"})
    api.post(API + "/applications/500/installations", json={"installation": {"installation_machine_id": 3}})
    api.post(API + "/applications/500/users", json={"application_users": []})
    api.delete(API + "/applications/200/users")
    api.delete(API + "/applications/201/users")
    result = apply_plan(client, plan)
    statuses: dict[str, list[str]] = defaultdict(list)
    for a in result.actions:
        statuses[a.action].append(a.status)
    assert statuses["create_software"] == ["done"]
    assert statuses["add_installation"] == ["done", "failed", "done"]
    assert statuses["remove_installations"] == ["skipped", "skipped"]
    assert statuses["delete_software"] == ["skipped", "skipped"]
    assert not any(method == "DELETE" and "/installations" in url for method, url, _ in sent(api))


# -- software delete --------------------------------------------------------------------------


def test_software_delete_keeps_used_entries(client: FreshServiceClient, api: responses.RequestsMock) -> None:
    with pytest.raises(ValueError, match="Select software"):
        plan_software_delete(client)
    pages(api, "/vendors", "vendors", [{"id": 11, "name": "Google"}])
    pages(
        api,
        "/applications",
        "applications",
        [
            {"id": 1, "name": "Chrome old", "publisher_id": 11, "installation_count": 0, "user_count": 0},
            {"id": 2, "name": "Chrome", "publisher_id": 11, "installation_count": 4, "user_count": 0},
            {
                "id": 3,
                "name": "Chrome licensed",
                "publisher_id": 11,
                "installation_count": 0,
                "user_count": 0,
            },
        ],
    )
    pages(api, "/applications/1/licenses", "licenses", [])
    pages(api, "/applications/2/licenses", "licenses", [])
    pages(api, "/applications/3/licenses", "licenses", [{"id": 9}])
    plan = plan_software_delete(client, vendor="Google")
    assert [a.path for a in plan.actions] == ["/applications/1"]
    assert plan.summary == {"selected": 3, "to_delete": 1, "kept_in_use": 2, "include_used": False}
