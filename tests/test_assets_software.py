from __future__ import annotations

import pytest
import responses

from FreshService import (
    FreshServiceClient,
    asset_type_tree,
    by_device,
    list_assets,
    list_devices,
    list_my_articles,
    list_software,
    product_vendor_fields,
    summarise_installs,
    where_installed,
)
from tests.conftest import API, asset, get, pages, register_asset_types, writes

VENDORS = [{"id": 10, "name": "Mozilla"}, {"id": 11, "name": "Google LLC"}, {"id": 12, "name": "Google"}]
SOFTWARE = [
    {"id": 100, "name": "Mozilla Firefox", "publisher_id": 10, "installation_count": 3},
    {"id": 101, "name": "Mozilla Thunderbird", "publisher_id": 10, "installation_count": 0},
    {"id": 102, "name": "Google Chrome", "publisher_id": 11, "installation_count": 1},
    {"id": 103, "name": "Untriaged thing", "publisher_id": None, "status": None},
]


def test_asset_type_tree(client: FreshServiceClient, api: responses.RequestsMock) -> None:
    register_asset_types(api)
    tree = asset_type_tree(client)
    assert tree.select(["Network"]) == {3, 4, 5}
    assert tree.select(["hardware"]) == {1, 2, 3, 4, 5}
    assert tree.path(4) == "Hardware > Network > Firewall"
    assert tree.ancestors(5) == [5, 3, 1]
    with pytest.raises(ValueError, match="No asset type named 'Router'"):
        tree.resolve("Router")
    asset_type_tree(client)
    assert len(api.calls) == 4  # cached: 6 types on 3 full pages (+1 empty), fetched once


def test_product_vendor_fields_come_from_the_type(
    client: FreshServiceClient, api: responses.RequestsMock
) -> None:
    register_asset_types(api, 4)
    names = product_vendor_fields(client, 4)
    assert (names.product, names.vendor, names.state) == ("product_1", "vendor_1", "asset_state_1")


def test_list_assets_by_type_filters_the_full_listing(
    client: FreshServiceClient, api: responses.RequestsMock
) -> None:
    register_asset_types(api)
    rows = [asset(1, "fw01", 4, vendor=10), asset(2, "pc01", 2), asset(3, "svc", 9), asset(4, "sw01", 5)]
    pages(api, "/assets", "assets", rows, {"include": "type_fields"})
    network = list_assets(client, types=["Network"], include_type_fields=True)
    assert [a.name for a in network] == ["fw01", "sw01"]
    assert network[0].vendor_id == 10 and network[0].product_id is None and network[0].asset_state == "In Use"
    assert [a.name for a in list_devices(client)] == ["fw01", "pc01", "sw01"]  # default: Hardware


def test_list_software_filters(client: FreshServiceClient, api: responses.RequestsMock) -> None:
    pages(api, "/applications", "applications", SOFTWARE)
    assert [s.id for s in list_software(client, vendor="google*", vendors=[])] == []
    from FreshService import Vendor

    vendors = [Vendor.model_validate(v) for v in VENDORS]
    assert [s.id for s in list_software(client, vendor="google*", vendors=vendors)] == [102]
    assert [s.id for s in list_software(client, name="mozilla", vendors=vendors)] == [100, 101]
    assert [s.id for s in list_software(client, status="none")] == [100, 101, 102, 103]


def test_where_installed_joins_devices(client: FreshServiceClient, api: responses.RequestsMock) -> None:
    pages(api, "/vendors", "vendors", VENDORS)
    pages(api, "/applications", "applications", SOFTWARE)
    register_asset_types(api)
    pages(
        api,
        "/applications/100/installations",
        "installations",
        [
            {"installation_machine_id": 1, "version": "127.0.1", "user_id": 5},
            {"installation_machine_id": 2, "version": "128.0"},
            {"installation_machine_id": 404, "version": "115.0esr"},
        ],
    )
    get(api, "/assets/1", {"asset": asset(1, "pc01", 2)}, {"include": "type_fields"})
    api.get(API + "/assets/404", status=404)

    installs = where_installed(client, vendor="mozilla", version="<128")
    assert [(i.device_id, i.device_name, i.version) for i in installs] == [
        (404, None, "115.0esr"),
        (1, "pc01", "127.0.1"),
    ]
    pc = installs[1]
    assert pc.vendor_name == "Mozilla" and pc.asset_type_name == "Computer" and pc.device_state == "In Use"
    assert pc.url == "https://fs.test/cmdb/items/1"
    # Thunderbird reports 0 installations, so it is never asked for; nothing is written.
    assert not any("/applications/101/" in (c.request.url or "") for c in api.calls)
    assert writes(api) == []

    summary = summarise_installs(installs)
    assert summary[0].device_count == 2 and summary[0].versions == {"127.0.1": 1, "115.0esr": 1}
    assert [d.device_id for d in by_device(installs)] == [404, 1]


def test_where_installed_rejects_a_bad_version_before_calling(client: FreshServiceClient) -> None:
    with pytest.raises(ValueError, match="not a version specifier"):
        where_installed(client, name="x", version="<<1")


def test_list_my_articles_walks_the_knowledge_base(
    client: FreshServiceClient, api: responses.RequestsMock
) -> None:
    client.settings.agent_email = "Me@Example.com"
    pages(api, "/agents", "agents", [{"id": 7, "email": "me@example.com"}], {"email": "Me@Example.com"})
    pages(api, "/solutions/categories", "categories", [{"id": 1, "name": "IT"}])
    pages(api, "/solutions/folders", "folders", [{"id": 20, "name": "VPN"}], {"category_id": 1})
    get(api, "/solutions/folders/20/sub-folders", {"sub_folders": [{"id": 21, "name": "Mac"}]})
    get(api, "/solutions/folders/21/sub-folders", {"sub_folders": []})
    pages(
        api,
        "/solutions/articles",
        "articles",
        [{"id": 1, "agent_id": 7}, {"id": 2, "agent_id": 8}],
        {"folder_id": 20},
    )
    pages(api, "/solutions/articles", "articles", [{"id": 3, "agent_id": 7, "status": 2}], {"folder_id": 21})
    assert [a.id for a in list_my_articles(client)] == [1, 3]
    assert [a.id for a in list_my_articles(client, agent_id=7, status=2)] == [3]


def test_alert_query() -> None:
    from datetime import date

    from FreshService import alert_query

    assert alert_query() is None
    assert alert_query(severity=["critical", "error"], state=["open"], since=date(2026, 11, 1)) == (
        "(severity:201 OR severity:151) AND state:1 AND occurrence_time:>'2026-11-01T00:00:00Z'"
    )
