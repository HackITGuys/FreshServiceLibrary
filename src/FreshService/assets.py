"""Assets (the classic CMDB), asset types, and devices.

PRODUCT AND VENDOR live in an asset's `type_fields`, under names that carry the id of
the asset type that defines them: product_636, vendor_636, asset_state_636. The id is
usually that of an ancestor ("Hardware"), not of the asset's own type ("Firewall").
product_vendor_fields() reads them from /asset_types/{id}/fields instead of guessing.

FILTERS. The asset filter API (`filter=`) can't query on product or vendor and stops at
40 pages (1200 assets). Listing by asset type is therefore done locally on the full
listing, which has no cap.

DEVICES are assets whose type is one of FRESH_DEVICE_ASSET_TYPES (default "Hardware")
or a descendant of one. Accounts created after 31 March 2026 have a separate
/itam/devices API, which this library does not use.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from FreshService._util import drop_none, quoted, workspace
from FreshService.client import FreshServiceClient, FreshServiceNotFoundError
from FreshService.models import Asset, AssetType, AssetTypeField

ASSET_FILTER_MAX_PAGES = 40
FETCH_ONE_BY_ONE = 50
"""Up to this many assets are fetched by id; more and one full listing is cheaper."""


# -- asset types -----------------------------------------------------------------


@dataclass
class AssetTypeTree:
    """All asset types, with parent/child navigation."""

    types: dict[int, AssetType]
    children: dict[int, list[int]] = field(default_factory=dict)

    @classmethod
    def build(cls, types: Iterable[AssetType]) -> AssetTypeTree:
        by_id = {asset_type.id: asset_type for asset_type in types}
        children: dict[int, list[int]] = defaultdict(list)
        for asset_type in by_id.values():
            if asset_type.parent_asset_type_id is not None:
                children[asset_type.parent_asset_type_id].append(asset_type.id)
        return cls(by_id, dict(children))

    def get(self, type_id: int | None) -> AssetType | None:
        return self.types.get(type_id) if type_id is not None else None

    def name(self, type_id: int | None) -> str | None:
        asset_type = self.get(type_id)
        return asset_type.name if asset_type else None

    def resolve(self, name_or_id: str | int) -> AssetType:
        """An asset type by id or by (case-insensitive) name."""
        text = str(name_or_id).strip()
        if text.isdigit() and int(text) in self.types:
            return self.types[int(text)]
        matches = [t for t in self.types.values() if t.name.casefold() == text.casefold()]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise ValueError(f"No asset type named {text!r}")
        ids = ", ".join(f"{t.id} ({self.path(t.id)})" for t in matches)
        raise ValueError(f"Several asset types are named {text!r}: {ids}. Use the id.")

    def descendants(self, type_id: int) -> set[int]:
        """The type and every type below it."""
        found = {type_id}
        stack = [type_id]
        while stack:
            for child in self.children.get(stack.pop(), []):
                if child not in found:
                    found.add(child)
                    stack.append(child)
        return found

    def ancestors(self, type_id: int) -> list[int]:
        """The type and its parents, nearest first."""
        chain: list[int] = []
        current: int | None = type_id
        while current is not None and current not in chain:
            chain.append(current)
            parent = self.types.get(current)
            current = parent.parent_asset_type_id if parent else None
        return chain

    def path(self, type_id: int) -> str:
        """ "Hardware > Network > Firewall"."""
        return " > ".join(self.name(t) or str(t) for t in reversed(self.ancestors(type_id)))

    def select(self, names_or_ids: Iterable[str | int]) -> set[int]:
        """The named types and all their descendants."""
        selected: set[int] = set()
        for name in names_or_ids:
            selected |= self.descendants(self.resolve(name).id)
        return selected


def list_asset_types(client: FreshServiceClient) -> list[AssetType]:
    return [AssetType.model_validate(raw) for raw in client.paginate("/asset_types", "asset_types")]


def asset_type_tree(client: FreshServiceClient) -> AssetTypeTree:
    """All asset types (cached on the client for its lifetime)."""
    return client.memo("asset_type_tree", lambda: AssetTypeTree.build(list_asset_types(client)))


def asset_type_fields(client: FreshServiceClient, type_id: int) -> list[AssetTypeField]:
    """Every field of an asset type's form, inherited ones included (cached per type)."""

    def fetch() -> list[AssetTypeField]:
        sections = client.get_list(f"/asset_types/{type_id}/fields", "asset_type_fields")
        return [
            AssetTypeField.model_validate({**raw, "field_header": section.get("field_header")})
            for section in sections
            for raw in section.get("fields") or []
        ]

    return client.memo(f"asset_type_fields:{type_id}", fetch)


@dataclass(frozen=True)
class TypeFieldNames:
    """The type_fields names for product, vendor and state on one asset type (None: not on it)."""

    product: str | None
    vendor: str | None
    state: str | None


def product_vendor_fields(client: FreshServiceClient, type_id: int) -> TypeFieldNames:
    fields = {f.base_name: f.name for f in asset_type_fields(client, type_id) if f.base_name != f.name}
    return TypeFieldNames(fields.get("product"), fields.get("vendor"), fields.get("asset_state"))


# -- assets ------------------------------------------------------------------------


def list_assets(
    client: FreshServiceClient,
    *,
    types: Sequence[str | int] | None = None,
    include_type_fields: bool = False,
    filter: str | None = None,
    search: str | None = None,
    trashed: bool = False,
    workspace_id: int | None = None,
) -> list[Asset]:
    """Assets, optionally of some asset types (and their child types).

    filter: the API's filter query ("asset_state:'IN USE' AND location_id:3"), capped at
    1200 results (FreshServiceTruncatedError beyond). search: name/asset_tag/serial_number/
    mac_addresses/ip_addresses/uuid search ("name:'fw-'"). include_type_fields costs
    2 extra API credits per call but is needed for product, vendor and state.
    """
    params: dict[str, Any] = drop_none(
        {
            "include": "type_fields" if include_type_fields else None,
            "workspace_id": workspace(client, workspace_id),
            "trashed": "true" if trashed else None,
        }
    )
    if filter or search:
        if filter:
            params["filter"] = quoted(filter)
        if search:
            params["search"] = quoted(search)
        raws = client.paginate_query("/assets", "assets", params, max_pages=ASSET_FILTER_MAX_PAGES)
    else:
        raws = client.paginate("/assets", "assets", params)
    assets = [Asset.model_validate(raw) for raw in raws]
    if types:
        wanted = asset_type_tree(client).select(types)
        assets = [asset for asset in assets if asset.asset_type_id in wanted]
    return assets


def list_devices(
    client: FreshServiceClient, *, types: Sequence[str | int] | None = None, workspace_id: int | None = None
) -> list[Asset]:
    """Assets of the device types (FRESH_DEVICE_ASSET_TYPES unless `types`), with type_fields."""
    return list_assets(
        client,
        types=list(types) if types else client.settings.device_asset_types,
        include_type_fields=True,
        workspace_id=workspace_id,
    )


def get_asset(client: FreshServiceClient, display_id: int, *, include_type_fields: bool = True) -> Asset:
    params = {"include": "type_fields"} if include_type_fields else None
    return Asset.model_validate(client.get_object(f"/assets/{display_id}", "asset", params))


def assets_by_display_id(
    client: FreshServiceClient, display_ids: Iterable[int], *, known: Mapping[int, Asset] | None = None
) -> dict[int, Asset]:
    """Assets for these display ids (with type_fields): fetched one by one when there are few,
    else from one full listing. Ids that no longer exist are left out."""
    wanted = set(display_ids)
    found: dict[int, Asset] = {i: a for i, a in (known or {}).items() if i in wanted}
    missing = sorted(wanted - found.keys())
    if not missing:
        return found
    if len(missing) <= FETCH_ONE_BY_ONE:

        def fetch(display_id: int) -> Asset | None:
            try:
                return get_asset(client, display_id)
            except FreshServiceNotFoundError:
                return None

        for asset in client.parallel(fetch, missing):
            if asset is not None:
                found[asset.display_id] = asset
        return found
    # Not workspace_id=0: across workspaces the API returns only global fields, no type_fields.
    for asset in list_assets(client, include_type_fields=True):
        if asset.display_id in wanted:
            found[asset.display_id] = asset
    return found


def update_asset(client: FreshServiceClient, display_id: int, **fields: Any) -> Asset:
    """Change asset fields; type-specific ones go in type_fields={"product_636": 12, ...}."""
    return Asset.model_validate(client.put(f"/assets/{display_id}", drop_none(fields), key="asset"))


def set_asset_product_vendor(
    client: FreshServiceClient,
    asset: Asset,
    *,
    product_id: int | None = None,
    vendor_id: int | None = None,
) -> Asset:
    """Set the product and/or vendor of one asset, finding the right type_fields names."""
    if asset.asset_type_id is None:
        raise ValueError(f"Asset {asset.display_id} has no asset type")
    names = product_vendor_fields(client, asset.asset_type_id)
    type_fields: dict[str, int] = {}
    if product_id is not None:
        if names.product is None:
            raise ValueError(f"Asset type {asset.asset_type_id} has no product field")
        type_fields[names.product] = product_id
    if vendor_id is not None:
        if names.vendor is None:
            raise ValueError(f"Asset type {asset.asset_type_id} has no vendor field")
        type_fields[names.vendor] = vendor_id
    if not type_fields:
        return asset
    return update_asset(client, asset.display_id, type_fields=type_fields)


def delete_asset(client: FreshServiceClient, display_id: int) -> None:
    """Move an asset to trash."""
    client.delete(f"/assets/{display_id}")


def asset_url(client: FreshServiceClient, display_id: int) -> str:
    return f"{client.settings.portal_root}/cmdb/items/{display_id}"
