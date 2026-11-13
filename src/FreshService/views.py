"""Brief views: just the identifying fields of each record, as flat rows.

    rows = brief(client, list_tickets(client))       # [{"id", "subject", "group", "state"}, ...]
    print(render_table(rows))

brief() picks the projection by record type. Ids are turned into names (group, vendor,
product, asset type) with one cached listing each. If the API key's role may not read a
listing, the id is shown instead of failing.

Brief rows never trigger per-record calls: software shows the API's own installation and
user counts, not installations.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from enum import IntEnum
from typing import Any

from pydantic import BaseModel

from FreshService.assets import asset_type_tree
from FreshService.catalog import list_products, list_vendors
from FreshService.client import FreshServiceAuthError, FreshServiceClient
from FreshService.models import (
    Agent,
    Alert,
    AlertSeverity,
    AlertState,
    Announcement,
    Article,
    ArticleStatus,
    Asset,
    CatalogItem,
    Change,
    ChangeStatus,
    Department,
    DeviceInstall,
    Group,
    Location,
    Product,
    Publisher,
    Role,
    Software,
    Ticket,
    TicketStatus,
    Vendor,
)
from FreshService.org import list_groups
from FreshService.software import version_rows

log = logging.getLogger("FreshService")

Row = dict[str, Any]


# -- name lookups ---------------------------------------------------------------------


def _lookup(client: FreshServiceClient, kind: str, fetch: Callable[[], dict[int, str]]) -> dict[int, str]:
    def safe() -> dict[int, str]:
        try:
            return fetch()
        except FreshServiceAuthError:
            log.info("No access to %s; showing ids instead of names", kind)
            return {}

    return client.memo(f"names:{kind}", safe)


def group_names(client: FreshServiceClient) -> dict[int, str]:
    return _lookup(client, "groups", lambda: {g.id: g.name for g in list_groups(client)})


def vendor_names(client: FreshServiceClient) -> dict[int, str]:
    return _lookup(client, "vendors", lambda: {v.id: v.name for v in list_vendors(client)})


def product_names(client: FreshServiceClient) -> dict[int, str]:
    return _lookup(client, "products", lambda: {p.id: p.name for p in list_products(client)})


def asset_type_names(client: FreshServiceClient) -> dict[int, str]:
    return _lookup(
        client, "asset types", lambda: {t.id: t.name for t in asset_type_tree(client).types.values()}
    )


def _name(names: Mapping[int, str], item_id: int | None) -> str | int | None:
    """The name for an id, else the id itself (unknown, or no access to the names)."""
    if item_id is None:
        return None
    return names.get(item_id, item_id)


def _code(enum: type[IntEnum], value: int | None) -> str | int | None:
    """open, pending, ... for built-in codes; the number for account-defined ones."""
    if value is None:
        return None
    try:
        return enum(value).name.lower()
    except ValueError:
        return value


# -- projections -------------------------------------------------------------------------


def brief_tickets(client: FreshServiceClient, tickets: Sequence[Ticket]) -> list[Row]:
    groups = group_names(client) if any(t.group_id for t in tickets) else {}
    return [
        {
            "id": t.id,
            "subject": t.subject,
            "group": _name(groups, t.group_id),
            "state": _code(TicketStatus, t.status),
        }
        for t in tickets
    ]


def brief_changes(client: FreshServiceClient, changes: Sequence[Change]) -> list[Row]:
    groups = group_names(client) if any(c.group_id for c in changes) else {}
    return [
        {
            "id": c.id,
            "subject": c.subject,
            "group": _name(groups, c.group_id),
            "state": _code(ChangeStatus, c.status),
        }
        for c in changes
    ]


def brief_articles(client: FreshServiceClient, articles: Sequence[Article]) -> list[Row]:
    return [
        {"id": a.id, "title": a.title, "state": _code(ArticleStatus, a.status), "folder_id": a.folder_id}
        for a in articles
    ]


def brief_announcements(client: FreshServiceClient, announcements: Sequence[Announcement]) -> list[Row]:
    return [{"id": a.id, "title": a.title, "state": a.state} for a in announcements]


def brief_alerts(client: FreshServiceClient, alerts: Sequence[Alert]) -> list[Row]:
    return [
        {
            "id": a.id,
            "subject": a.subject,
            "severity": _code(AlertSeverity, a.severity),
            "state": _code(AlertState, a.state),
            "resource": a.resource,
        }
        for a in alerts
    ]


def brief_catalog_items(client: FreshServiceClient, items: Sequence[CatalogItem]) -> list[Row]:
    return [{"id": i.display_id or i.id, "name": i.name, "category_id": i.category_id} for i in items]


def brief_agents(client: FreshServiceClient, agents: Sequence[Agent]) -> list[Row]:
    return [{"id": a.id, "name": a.name, "email": a.email, "active": a.active} for a in agents]


def brief_named(
    client: FreshServiceClient, records: Sequence[Group | Role | Department | Location]
) -> list[Row]:
    return [{"id": r.id, "name": r.name} for r in records]


def brief_assets(client: FreshServiceClient, assets: Sequence[Asset]) -> list[Row]:
    """Assets as id, name, type, vendor, product, state. Vendor, product and state come from
    type_fields: list the assets with include_type_fields=True (devices always are)."""
    types = asset_type_names(client)
    vendors = vendor_names(client) if any(a.vendor_id for a in assets) else {}
    products = product_names(client) if any(a.product_id for a in assets) else {}
    return [
        {
            "id": a.display_id,
            "name": a.name,
            "type": _name(types, a.asset_type_id),
            "vendor": _name(vendors, a.vendor_id),
            "product": _name(products, a.product_id),
            "state": a.asset_state,
        }
        for a in assets
    ]


def brief_products(client: FreshServiceClient, products: Sequence[Product]) -> list[Row]:
    types = asset_type_names(client)
    return [
        {"id": p.id, "name": p.name, "type": _name(types, p.asset_type_id), "manufacturer": p.manufacturer}
        for p in products
    ]


def brief_vendors(client: FreshServiceClient, vendors: Sequence[Vendor]) -> list[Row]:
    return [{"id": v.id, "name": v.name} for v in vendors]


def brief_publishers(client: FreshServiceClient, publishers: Sequence[Publisher]) -> list[Row]:
    return [
        {"id": p.id, "name": p.name, "software": p.software_count, "installs": p.installation_count}
        for p in publishers
    ]


def brief_software(client: FreshServiceClient, entries: Sequence[Software]) -> list[Row]:
    """Software as id, name, publisher, installs, users: the API's counts, no extra calls."""
    names = {} if all(e.publisher_name or not e.publisher_id for e in entries) else vendor_names(client)
    return [
        {
            "id": e.id,
            "name": e.name,
            "publisher": e.publisher_name or _name(names, e.publisher_id),
            "installs": e.installation_count,
            "users": e.user_count,
        }
        for e in entries
    ]


def brief_installs(client: FreshServiceClient, installs: Sequence[DeviceInstall]) -> list[Row]:
    """Installations as one row per software and version: publisher, software, version, devices."""
    return version_rows(installs)


BRIEF: dict[type[BaseModel], Callable[[FreshServiceClient, Sequence[Any]], list[Row]]] = {
    Ticket: brief_tickets,
    Change: brief_changes,
    Article: brief_articles,
    Announcement: brief_announcements,
    Alert: brief_alerts,
    CatalogItem: brief_catalog_items,
    Agent: brief_agents,
    Group: brief_named,
    Role: brief_named,
    Department: brief_named,
    Location: brief_named,
    Asset: brief_assets,
    Product: brief_products,
    Vendor: brief_vendors,
    Publisher: brief_publishers,
    Software: brief_software,
    DeviceInstall: brief_installs,
}


def brief(client: FreshServiceClient, records: Iterable[BaseModel]) -> list[Row]:
    """The brief rows for a list of records of one type (see the module docstring)."""
    items = list(records)
    if not items:
        return []
    for cls in type(items[0]).__mro__:
        projection = BRIEF.get(cls)
        if projection is not None:
            return projection(client, items)
    raise TypeError(f"No brief view for {type(items[0]).__name__}")


# -- tables --------------------------------------------------------------------------------


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list | tuple):
        return ", ".join(_cell(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, default=str)
    return " ".join(str(value).split())


def _numeric(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def render_table(rows: Sequence[Mapping[str, Any]]) -> str:
    """Rows as aligned text columns with upper-case headers. Numbers are right-aligned; None is
    an empty cell. Nothing is truncated, so the output greps cleanly."""
    if not rows:
        return ""
    columns: list[str] = []
    for row in rows:
        columns.extend(key for key in row if key not in columns)
    cells = [[_cell(row.get(column)) for column in columns] for row in rows]
    headers = [column.upper() for column in columns]
    widths = [max(len(header), *(len(line[i]) for line in cells)) for i, header in enumerate(headers)]
    right = [
        all(_numeric(row.get(column)) or row.get(column) is None for row in rows)
        and any(_numeric(row.get(column)) for row in rows)
        for column in columns
    ]

    def line(values: Sequence[str]) -> str:
        padded = [
            value.rjust(width) if align else value.ljust(width)
            for value, width, align in zip(values, widths, right, strict=True)
        ]
        return "  ".join(padded).rstrip()

    return "\n".join([line(headers), *(line(values) for values in cells)])
