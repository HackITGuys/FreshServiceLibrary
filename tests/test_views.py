from __future__ import annotations

import pytest
import responses

from FreshService import (
    Asset,
    DeviceInstall,
    FreshServiceClient,
    Software,
    Ticket,
    brief,
    render_table,
    software_usage,
    version_rows,
)
from tests.conftest import API, asset, pages, register_asset_types


def test_tickets_show_group_names_and_state_names(
    client: FreshServiceClient, api: responses.RequestsMock
) -> None:
    pages(api, "/groups", "groups", [{"id": 5, "name": "Network"}])
    tickets = [
        Ticket(id=1, subject="VPN down", group_id=5, status=2, description="<p>long</p>"),
        Ticket(id=2, subject="Custom state", group_id=None, status=99),
    ]
    assert brief(client, tickets) == [
        {"id": 1, "subject": "VPN down", "group": "Network", "state": "open"},
        {"id": 2, "subject": "Custom state", "group": None, "state": 99},
    ]


def test_names_fall_back_to_ids_without_access(
    client: FreshServiceClient, api: responses.RequestsMock
) -> None:
    api.get(API + "/groups", status=403, json={"code": "access_denied", "message": "denied"})
    rows = brief(client, [Ticket(id=1, subject="x", group_id=5, status=4)])
    assert rows == [{"id": 1, "subject": "x", "group": 5, "state": "resolved"}]
    brief(client, [Ticket(id=2, subject="y", group_id=5, status=4)])
    assert len(api.calls) == 1  # the failed lookup is remembered for the client's lifetime


def test_assets_show_type_vendor_product_and_state(
    client: FreshServiceClient, api: responses.RequestsMock
) -> None:
    register_asset_types(api)
    pages(api, "/vendors", "vendors", [{"id": 10, "name": "Fortinet"}])
    pages(api, "/products", "products", [{"id": 50, "name": "FortiGate 100F", "asset_type_id": 4}])
    assets = [
        Asset.model_validate(asset(1, "fw01", 4, product=50, vendor=10)),
        Asset.model_validate(asset(2, "fw02", 4, state=None)),
    ]
    assert brief(client, assets) == [
        {
            "id": 1,
            "name": "fw01",
            "type": "Firewall",
            "vendor": "Fortinet",
            "product": "FortiGate 100F",
            "state": "In Use",
        },
        {"id": 2, "name": "fw02", "type": "Firewall", "vendor": None, "product": None, "state": None},
    ]


def test_software_brief_needs_no_calls_when_names_are_known(client: FreshServiceClient) -> None:
    entries = [
        Software(
            id=1,
            name="Firefox",
            publisher_id=10,
            publisher_name="Mozilla",
            installation_count=3,
            user_count=1,
        ),
        Software(id=2, name="Mystery"),
    ]
    assert brief(client, entries) == [
        {"id": 1, "name": "Firefox", "publisher": "Mozilla", "installs": 3, "users": 1},
        {"id": 2, "name": "Mystery", "publisher": None, "installs": None, "users": None},
    ]
    assert brief(client, []) == []
    with pytest.raises(TypeError, match="No brief view"):
        brief(client, [Software.__mro__[1]()])  # a plain BaseModel


def test_software_usage_counts_devices_per_version(
    client: FreshServiceClient, api: responses.RequestsMock
) -> None:
    pages(
        api,
        "/applications/1/installations",
        "installations",
        [
            {"installation_machine_id": 1, "version": "9.0"},
            {"installation_machine_id": 2, "version": "10.0"},
            {"installation_machine_id": 2, "version": "10.0", "installation_path": "C:/other"},
            {"installation_machine_id": 3},
        ],
    )
    usage = software_usage(
        client,
        [
            Software(id=1, name="App", installation_count=4),
            Software(id=2, name="Unused", installation_count=0),
        ],
    )
    assert (usage[0].device_count, usage[0].versions) == (3, {"10.0": 1, "9.0": 1, "": 1})
    assert (usage[1].device_count, usage[1].versions) == (0, {})
    assert all("/applications/2/" not in (call.request.url or "") for call in api.calls)


def test_version_rows_flatten_where_results() -> None:
    def install(software_id: int, name: str, device: int, version: str | None) -> DeviceInstall:
        return DeviceInstall(
            software_id=software_id, software_name=name, vendor_name="V", device_id=device, version=version
        )

    rows = version_rows(
        [
            install(1, "Small", 9, "1.0"),
            install(2, "Big", 1, "2.0"),
            install(2, "Big", 2, "10.0"),
            install(2, "Big", 3, "10.0"),
        ]
    )
    assert rows == [
        {"publisher": "V", "software": "Big", "version": "10.0", "devices": 2},
        {"publisher": "V", "software": "Big", "version": "2.0", "devices": 1},
        {"publisher": "V", "software": "Small", "version": "1.0", "devices": 1},
    ]


def test_render_table() -> None:
    assert render_table([]) == ""
    table = render_table(
        [
            {"id": 7, "name": "fw01", "active": True, "note": None},
            {"id": 12345, "name": "a  very\nlong name", "active": False, "tags": ["a", "b"]},
        ]
    )
    assert table.splitlines() == [
        "   ID  NAME              ACTIVE  NOTE  TAGS",
        "    7  fw01              yes",
        "12345  a very long name  no            a, b",
    ]
