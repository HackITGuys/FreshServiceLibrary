"""Read-only calls against a real Freshservice account. Skipped by default.

uv run pytest -m live -rs       # needs FRESH_DOMAIN / FRESH_API_KEY (env or .env)

Nothing here writes. Write paths are exercised by the mocked tests; try them against a
test ticket/change/article by hand, and run maintenance commands without --apply first.

An endpoint the key's agent may not use (HTTP 403) is reported as a skip, not a failure:
what a key can do is decided by its agent's role. Any other error fails.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest

from FreshService import (
    FreshServiceAuthError,
    FreshServiceClient,
    FreshServiceSettings,
    asset_type_tree,
    list_agents,
    list_alerts,
    list_announcements,
    list_catalog_categories,
    list_categories,
    list_changes,
    list_departments,
    list_groups,
    list_locations,
    list_products,
    list_roles,
    list_software,
    list_tickets,
    list_vendors,
    product_vendor_fields,
    where_installed,
)

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def live_client() -> Iterator[FreshServiceClient]:
    settings = FreshServiceSettings()
    if settings.missing():
        pytest.skip(f"{', '.join(settings.missing())} not set")
    with FreshServiceClient(settings) as client:
        yield client


READS: list[tuple[str, Callable[[FreshServiceClient], Any]]] = [
    ("agents", lambda c: list_agents(c, active=True)),
    ("groups", list_groups),
    ("roles", list_roles),
    ("departments", list_departments),
    ("locations", list_locations),
    ("tickets", list_tickets),
    ("changes", list_changes),
    ("announcements", lambda c: list_announcements(c, state="active")),
    ("solution categories", list_categories),
    ("catalog categories", list_catalog_categories),
    ("alerts", lambda c: list_alerts(c, "state:1")),
    ("vendors", list_vendors),
    ("products", list_products),
]


@pytest.mark.parametrize(("name", "read"), READS, ids=[name for name, _ in READS])
def test_list_endpoint(
    live_client: FreshServiceClient, name: str, read: Callable[[FreshServiceClient], Any]
) -> None:
    try:
        records = read(live_client)
    except FreshServiceAuthError as exc:
        pytest.skip(f"{name}: not allowed for this key ({exc.status})")
    assert isinstance(records, list)


def test_assets_and_software(live_client: FreshServiceClient) -> None:
    tree = asset_type_tree(live_client)
    hardware = tree.resolve(live_client.settings.device_asset_types[0])
    fields = product_vendor_fields(live_client, hardware.id)
    assert fields.product and fields.vendor, "Hardware types carry product and vendor fields"
    vendors = list_vendors(live_client)
    software = list_software(live_client, vendors=vendors)
    assert software, "an account with discovery has software"
    small = [entry for entry in software if 0 < (entry.installation_count or 0) <= 20]
    if not small:
        pytest.skip("no software entry with 1-20 installations to sample")
    installs = where_installed(live_client, software=small[:1], vendors=vendors)
    assert installs and all(install.software_id == small[0].id for install in installs)
    assert any(install.device_name for install in installs)
