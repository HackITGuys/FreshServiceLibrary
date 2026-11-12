"""Products (asset models) and vendors.

A product belongs to exactly one asset type and has a free-text `manufacturer`; there is
no product -> vendor link. Vendors are what software `publisher_id` and the asset
`vendor_<n>` field point at. Neither has a merge endpoint (see maintenance.py).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from FreshService._util import drop_none
from FreshService.client import FreshServiceClient
from FreshService.matching import name_matches, text_matches
from FreshService.models import Contract, Product, Vendor

# -- products -------------------------------------------------------------------


def list_products(
    client: FreshServiceClient, *, asset_type_id: int | None = None, name: str | None = None
) -> list[Product]:
    """Products, optionally of one asset type and/or whose name matches (substring or glob)."""
    products = [Product.model_validate(raw) for raw in client.paginate("/products", "products")]
    return [
        product
        for product in products
        if (asset_type_id is None or product.asset_type_id == asset_type_id)
        and text_matches(product.name, name)
    ]


def get_product(client: FreshServiceClient, product_id: int) -> Product:
    return Product.model_validate(client.get_object(f"/products/{product_id}", "product"))


def create_product(
    client: FreshServiceClient,
    *,
    name: str,
    asset_type_id: int,
    manufacturer: str | None = None,
    **fields: Any,
) -> Product:
    """fields: status (In Production/In Pipeline/Retired), mode_of_procurement (Buy/Lease/Both),
    depreciation_type_id, description."""
    body = drop_none({"name": name, "asset_type_id": asset_type_id, "manufacturer": manufacturer, **fields})
    return Product.model_validate(client.post("/products", body, key="product"))


def update_product(client: FreshServiceClient, product_id: int, **fields: Any) -> Product:
    return Product.model_validate(client.put(f"/products/{product_id}", drop_none(fields), key="product"))


def delete_product(client: FreshServiceClient, product_id: int) -> None:
    client.delete(f"/products/{product_id}")


def find_product(products: Iterable[Product], name: str, asset_type_ids: Iterable[int]) -> Product | None:
    """The product with this exact (case-insensitive) name for the first of asset_type_ids
    (nearest type first) that has one."""
    by_type: dict[int, Product] = {}
    for product in products:
        if product.asset_type_id is not None and name_matches(product.name, name):
            by_type.setdefault(product.asset_type_id, product)
    for type_id in asset_type_ids:
        if type_id in by_type:
            return by_type[type_id]
    return None


# -- vendors --------------------------------------------------------------------------


def list_vendors(client: FreshServiceClient, *, name: str | None = None) -> list[Vendor]:
    """Vendors, optionally whose name matches (substring or glob)."""
    vendors = [Vendor.model_validate(raw) for raw in client.paginate("/vendors", "vendors")]
    return [vendor for vendor in vendors if text_matches(vendor.name, name)]


def get_vendor(client: FreshServiceClient, vendor_id: int) -> Vendor:
    return Vendor.model_validate(client.get_object(f"/vendors/{vendor_id}", "vendor"))


def create_vendor(client: FreshServiceClient, *, name: str, **fields: Any) -> Vendor:
    """fields: description, primary_contact_id, address {line1, city, state, country, zipcode}."""
    return Vendor.model_validate(client.post("/vendors", drop_none({"name": name, **fields}), key="vendor"))


def update_vendor(client: FreshServiceClient, vendor_id: int, **fields: Any) -> Vendor:
    return Vendor.model_validate(client.put(f"/vendors/{vendor_id}", drop_none(fields), key="vendor"))


def delete_vendor(client: FreshServiceClient, vendor_id: int) -> None:
    client.delete(f"/vendors/{vendor_id}")


def find_vendor(vendors: Iterable[Vendor], name: str) -> Vendor | None:
    """The vendor with this exact (case-insensitive) name."""
    for vendor in vendors:
        if name_matches(vendor.name, name):
            return vendor
    return None


def resolve_vendors(vendors: Iterable[Vendor], names_or_ids: Iterable[str | int]) -> list[Vendor]:
    """Vendors by id, exact name or glob. Each argument must match at least one."""
    pool = list(vendors)
    by_id = {vendor.id: vendor for vendor in pool}
    found: dict[int, Vendor] = {}
    for token in names_or_ids:
        text = str(token).strip()
        if text.isdigit() and int(text) in by_id:
            found[int(text)] = by_id[int(text)]
            continue
        matches = [vendor for vendor in pool if name_matches(vendor.name, text)]
        if not matches:
            raise ValueError(f"No vendor matches {text!r}")
        for vendor in matches:
            found[vendor.id] = vendor
    return list(found.values())


def find_or_create_vendor(client: FreshServiceClient, name: str) -> Vendor:
    existing = find_vendor(list_vendors(client), name)
    return existing or create_vendor(client, name=name)


def find_or_create_product(
    client: FreshServiceClient, name: str, asset_type_id: int, *, manufacturer: str | None = None
) -> Product:
    existing = find_product(list_products(client), name, [asset_type_id])
    return existing or create_product(
        client, name=name, asset_type_id=asset_type_id, manufacturer=manufacturer
    )


# -- contracts (read; vendor merges re-point them) ----------------------------------------


def list_contracts(client: FreshServiceClient) -> list[Contract]:
    return [Contract.model_validate(raw) for raw in client.paginate("/contracts", "contracts")]
