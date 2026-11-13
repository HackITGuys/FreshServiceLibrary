"""Bulk maintenance, planned before it is applied.

Every operation is split in two. plan_*() only reads and returns a MaintenancePlan: the
list of writes it would make, with before/after values. apply_plan() then makes them.
The CLI prints the plan unless --apply is given.

    plan = plan_tag_unknown(client, ["Firewall", "Switch"])
    print(plan.model_dump_json(indent=2))          # review
    result = apply_plan(client, plan)               # write

An object a plan creates (an "Unknown" vendor, a merge target) is referred to by later
actions as `<ref:NAME>` until it exists. An action whose prerequisites failed is skipped,
never guessed at, so a partly failed run can simply be planned and applied again.

None of this is atomic: Freshservice has no merge or transaction endpoints.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from FreshService.assets import (
    asset_type_tree,
    assets_by_display_id,
    list_assets,
    product_vendor_fields,
)
from FreshService.catalog import (
    find_product,
    find_vendor,
    list_contracts,
    list_products,
    list_vendors,
    resolve_vendors,
)
from FreshService.client import (
    FreshServiceAuthError,
    FreshServiceClient,
    FreshServiceError,
    FreshServiceNotFoundError,
)
from FreshService.matching import is_glob, name_matches, text_matches, version_key
from FreshService.models import (
    Asset,
    Installation,
    MaintenancePlan,
    PlannedAction,
    Software,
    SoftwareUser,
    Vendor,
    type_field,
)
from FreshService.software import (
    ID_CHUNK,
    list_installations,
    list_licenses,
    list_software,
    list_software_users,
    publisher_ids,
)

_REF = re.compile(r"<ref:([^>]+)>")
REDISCOVERY_WARNING = (
    "Discovery agents and probes may re-create a merged-away software entry on their next scan; "
    "the merge is safe to run again."
)


class _Builder:
    def __init__(self, operation: str) -> None:
        self.plan = MaintenancePlan(operation=operation)

    def add(self, **fields: Any) -> int:
        self.plan.actions.append(PlannedAction(**fields))
        return len(self.plan.actions) - 1

    def warn(self, message: str) -> None:
        if message not in self.plan.warnings:
            self.plan.warnings.append(message)


def ref(name: str) -> str:
    return f"<ref:{name}>"


def _substitute(value: Any, refs: dict[str, int]) -> Any:
    """Replace <ref:NAME> placeholders; a whole-value placeholder becomes the int id."""
    if isinstance(value, str):
        whole = _REF.fullmatch(value)
        if whole:
            return refs[whole.group(1)]
        return _REF.sub(lambda match: str(refs[match.group(1)]), value)
    if isinstance(value, dict):
        return {key: _substitute(item, refs) for key, item in value.items()}
    if isinstance(value, list):
        return [_substitute(item, refs) for item in value]
    return value


def apply_plan(client: FreshServiceClient, plan: MaintenancePlan) -> MaintenancePlan:
    """Make the plan's writes in order. Returns a copy with each action's status."""
    result = plan.model_copy(deep=True)
    refs: dict[str, int] = {}
    for done, action in enumerate(result.actions, start=1):
        client.report("applying", done, len(result.actions))
        if action.status != "planned":
            continue
        blocked = [index for index in action.requires if result.actions[index].status != "done"]
        if blocked:
            action.status, action.error = "skipped", f"depends on actions {blocked}, which did not succeed"
            continue
        try:
            path = _substitute(action.path, refs)
            body = _substitute(action.body, refs)
            params = _substitute(action.params, refs)
        except KeyError as exc:
            action.status, action.error = "skipped", f"needs the id of {exc.args[0]!r}, which was not created"
            continue
        try:
            if action.method == "DELETE":
                client.delete(path, params)
            else:
                created = client.send(action.method, path, action.response_key, body, params=params)
                if action.ref:
                    new_id = created.get("id") if isinstance(created, dict) else None
                    if new_id is None:
                        raise FreshServiceError(f"{action.method} {path} returned no id for {action.ref!r}")
                    refs[action.ref] = action.result_id = int(new_id)
            action.status = "done"
        except FreshServiceError as exc:
            action.status, action.error = "failed", str(exc)
    result.applied = True
    for status in ("done", "failed", "skipped"):
        result.summary[status] = sum(1 for action in result.actions if action.status == status)
    return result


def _asset_label(asset: Asset, type_name: str | None) -> str:
    return f"asset {asset.display_id} {asset.name!r} ({type_name or asset.asset_type_id})"


def unknown_product_name(label: str, type_name: str) -> str:
    """Placeholder products are per asset type ("Unknown (Firewall)"): a product belongs to
    one type, and a shared prefix keeps them filterable together."""
    return f"{label} ({type_name})"


# -- placeholder product/vendor ------------------------------------------------------


def plan_tag_unknown(
    client: FreshServiceClient,
    types: Sequence[str | int],
    *,
    label: str | None = None,
    fill: Iterable[str] = ("product", "vendor"),
) -> MaintenancePlan:
    """Give every asset of these types (and child types) that has no product and/or no
    vendor the placeholder ones: vendor "Unknown" and product "Unknown (<type>)", created
    if needed. Assets that already have a value keep it."""
    label = label or client.settings.unknown_label
    wanted = set(fill)
    if not wanted <= {"product", "vendor"}:
        raise ValueError("fill takes 'product' and/or 'vendor'")
    tree = asset_type_tree(client)
    type_ids = tree.select(types)
    assets = list_assets(client, types=list(types), include_type_fields=True)
    vendors = list_vendors(client)
    products = list_products(client)
    build = _Builder("tag-unknown")

    vendor_value: list[int | str] = []

    def unknown_vendor() -> int | str:
        if not vendor_value:
            existing = find_vendor(vendors, label)
            if existing:
                vendor_value.append(existing.id)
            else:
                build.add(
                    action="create_vendor",
                    target=f"vendor {label!r}",
                    method="POST",
                    path="/vendors",
                    body={
                        "name": label,
                        "description": "Placeholder for assets whose vendor is not known yet.",
                    },
                    response_key="vendor",
                    ref=f"vendor:{label}",
                )
                vendor_value.append(ref(f"vendor:{label}"))
        return vendor_value[0]

    product_values: dict[int, int | str] = {}

    def unknown_product(type_id: int) -> int | str:
        if type_id not in product_values:
            name = unknown_product_name(label, tree.name(type_id) or str(type_id))
            existing = find_product(products, name, [type_id])
            if existing:
                product_values[type_id] = existing.id
            else:
                build.add(
                    action="create_product",
                    target=f"product {name!r} for asset type {tree.path(type_id)}",
                    method="POST",
                    path="/products",
                    body={"name": name, "asset_type_id": type_id, "manufacturer": label},
                    response_key="product",
                    ref=f"product:{type_id}",
                )
                product_values[type_id] = ref(f"product:{type_id}")
        return product_values[type_id]

    updates = 0
    for asset in sorted(assets, key=lambda a: (a.asset_type_id or 0, a.name.casefold())):
        if asset.asset_type_id is None:
            continue
        names = product_vendor_fields(client, asset.asset_type_id)
        type_name = tree.name(asset.asset_type_id)
        if names.product is None and names.vendor is None:
            build.warn(f"Asset type {type_name!r} has no product or vendor field; its assets are skipped")
            continue
        changes: dict[str, int | str] = {}
        if "product" in wanted and names.product and asset.product_id is None:
            changes[names.product] = unknown_product(asset.asset_type_id)
        if "vendor" in wanted and names.vendor and asset.vendor_id is None:
            changes[names.vendor] = unknown_vendor()
        if changes:
            updates += 1
            build.add(
                action="update_asset",
                target=_asset_label(asset, type_name),
                method="PUT",
                path=f"/assets/{asset.display_id}",
                body={"type_fields": changes},
                response_key="asset",
                before={name: asset.type_fields.get(name) for name in changes},
                after=dict(changes),
            )
    build.plan.summary = {
        "asset_types": sorted(tree.path(type_id) for type_id in type_ids),
        "assets_scanned": len(assets),
        "assets_to_update": updates,
        "label": label,
    }
    return build.plan


def plan_set_product_vendor(
    client: FreshServiceClient,
    *,
    product: str | None = None,
    vendor: str | None = None,
    display_ids: Sequence[int] = (),
    types: Sequence[str | int] = (),
    name: str | None = None,
    current_product: str | None = None,
    current_vendor: str | None = None,
    manufacturer: str | None = None,
    create_missing: bool = False,
) -> MaintenancePlan:
    """Set product and/or vendor (by name) on the selected assets.

    Select by display_ids and/or types, name (substring/glob), current_product and
    current_vendor (name substring/glob, or "none" for unset; e.g. "Unknown*" to pick up
    placeholders). A product is looked up for the asset's own type, then its parent
    types; create_missing creates missing products (for the asset's own type) and vendors.
    """
    if product is None and vendor is None:
        raise ValueError("Give a product and/or a vendor to set")
    if not (display_ids or types or name or current_product or current_vendor):
        raise ValueError("Select assets: display ids, types, name, current_product or current_vendor")
    tree = asset_type_tree(client)
    if display_ids:
        found = assets_by_display_id(client, display_ids)
        assets = list(found.values())
        build_missing = sorted(set(display_ids) - found.keys())
        if types:
            allowed = tree.select(types)
            assets = [asset for asset in assets if asset.asset_type_id in allowed]
    else:
        assets = list_assets(client, types=list(types) or None, include_type_fields=True)
        build_missing = []
    products = list_products(client)
    vendors = list_vendors(client)
    product_names = {item.id: item.name for item in products}
    vendor_names = {item.id: item.name for item in vendors}
    build = _Builder("set-product-vendor")
    for missing in build_missing:
        build.warn(f"No asset with display id {missing}")

    def current_matches(current_id: int | None, names: dict[int, str], pattern: str | None) -> bool:
        if pattern is None:
            return True
        if pattern.strip().lower() == "none":
            return current_id is None
        return current_id is not None and text_matches(names.get(current_id), pattern)

    selected = [
        asset
        for asset in assets
        if asset.asset_type_id is not None
        and text_matches(asset.name, name)
        and current_matches(asset.product_id, product_names, current_product)
        and current_matches(asset.vendor_id, vendor_names, current_vendor)
    ]

    vendor_value: int | str | None = None
    if vendor is not None:
        existing_vendor = find_vendor(vendors, vendor)
        if existing_vendor:
            vendor_value = existing_vendor.id
        elif create_missing:
            build.add(
                action="create_vendor",
                target=f"vendor {vendor!r}",
                method="POST",
                path="/vendors",
                body={"name": vendor},
                response_key="vendor",
                ref=f"vendor:{vendor}",
            )
            vendor_value = ref(f"vendor:{vendor}")
        else:
            raise ValueError(
                f"No vendor named {vendor!r}; pass create_missing (--create-missing) to create it"
            )

    product_values: dict[int, int | str | None] = {}

    def product_for(type_id: int) -> int | str | None:
        assert product is not None
        if type_id not in product_values:
            existing = find_product(products, product, tree.ancestors(type_id))
            if existing:
                product_values[type_id] = existing.id
            elif create_missing:
                build.add(
                    action="create_product",
                    target=f"product {product!r} for asset type {tree.path(type_id)}",
                    method="POST",
                    path="/products",
                    body={"name": product, "asset_type_id": type_id, "manufacturer": manufacturer or vendor},
                    response_key="product",
                    ref=f"product:{type_id}:{product}",
                )
                product_values[type_id] = ref(f"product:{type_id}:{product}")
            else:
                build.warn(
                    f"No product {product!r} for asset type {tree.path(type_id)}; its assets keep their "
                    f"product (pass --create-missing to create it)"
                )
                product_values[type_id] = None
        return product_values[type_id]

    updates = 0
    for asset in sorted(selected, key=lambda a: a.name.casefold()):
        assert asset.asset_type_id is not None
        names = product_vendor_fields(client, asset.asset_type_id)
        changes: dict[str, int | str] = {}
        if product is not None and names.product:
            value = product_for(asset.asset_type_id)
            if value is not None and value != asset.product_id:
                changes[names.product] = value
        if vendor_value is not None and names.vendor and vendor_value != asset.vendor_id:
            changes[names.vendor] = vendor_value
        if not changes:
            continue
        updates += 1
        build.add(
            action="update_asset",
            target=_asset_label(asset, tree.name(asset.asset_type_id)),
            method="PUT",
            path=f"/assets/{asset.display_id}",
            body={"type_fields": changes},
            response_key="asset",
            before={
                "product": product_names.get(asset.product_id) if asset.product_id else None,
                "vendor": vendor_names.get(asset.vendor_id) if asset.vendor_id else None,
            },
            after={"product": product, "vendor": vendor},
        )
    build.plan.summary = {"assets_selected": len(selected), "assets_to_update": updates}
    return build.plan


# -- vendor merge -------------------------------------------------------------------------


def _one_vendor(vendors: Sequence[Vendor], name_or_id: str | int) -> Vendor:
    """Exactly one vendor, by id or exact (case-insensitive) name."""
    text = str(name_or_id).strip()
    if is_glob(text):
        raise ValueError(f"{text!r} is a pattern; name one vendor exactly (or by id)")
    if text.isdigit():
        for vendor in vendors:
            if vendor.id == int(text):
                return vendor
    matches = [vendor for vendor in vendors if name_matches(vendor.name, text)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(f"No vendor named {text!r}")
    raise ValueError(
        f"Several vendors are named {text!r}: {', '.join(str(v.id) for v in matches)}. Use the id."
    )


def plan_vendor_merge(
    client: FreshServiceClient,
    sources: Sequence[str | int],
    into: str | int,
    *,
    delete: bool = False,
    include_assets: bool = True,
    include_contracts: bool = True,
) -> MaintenancePlan:
    """Re-point everything that references the source vendors to `into`: software publisher,
    the asset vendor field (a full asset scan: the filter API can't query vendors) and
    contracts. Purchase orders are reported, not changed. With delete, each source vendor
    is deleted once everything that referenced it was re-pointed."""
    vendors = list_vendors(client)
    target = _one_vendor(vendors, into)
    source_vendors = [vendor for vendor in resolve_vendors(vendors, sources) if vendor.id != target.id]
    if not source_vendors:
        raise ValueError("No source vendors left once the target is excluded")
    source_ids = {vendor.id for vendor in source_vendors}
    names = {vendor.id: vendor.name for vendor in vendors}
    build = _Builder("vendor-merge")
    depends: dict[int, list[int]] = defaultdict(list)

    software = [entry for entry in list_software(client, vendors=vendors) if entry.publisher_id in source_ids]
    for entry in software:
        assert entry.publisher_id is not None
        depends[entry.publisher_id].append(
            build.add(
                action="update_software",
                target=f"software {entry.id} {entry.name!r}",
                method="PUT",
                path=f"/applications/{entry.id}",
                body={"publisher_id": target.id},
                response_key="application",
                before={"vendor": names.get(entry.publisher_id)},
                after={"vendor": target.name},
            )
        )

    asset_count = 0
    if include_assets:
        tree = asset_type_tree(client)
        for asset in list_assets(client, include_type_fields=True):
            vendor_id = asset.vendor_id
            field_name = type_field(asset.type_fields, "vendor")[0]
            if vendor_id not in source_ids or field_name is None:
                continue
            asset_count += 1
            depends[vendor_id].append(
                build.add(
                    action="update_asset",
                    target=_asset_label(asset, tree.name(asset.asset_type_id)),
                    method="PUT",
                    path=f"/assets/{asset.display_id}",
                    body={"type_fields": {field_name: target.id}},
                    response_key="asset",
                    before={"vendor": names.get(vendor_id)},
                    after={"vendor": target.name},
                )
            )

    contract_count = 0
    if include_contracts:
        try:
            contracts = list_contracts(client)
        except (FreshServiceAuthError, FreshServiceNotFoundError) as exc:
            build.warn(f"Contracts could not be read, so they are not re-pointed: {exc}")
            contracts = []
        for contract in contracts:
            if contract.vendor_id in source_ids:
                contract_count += 1
                depends[contract.vendor_id].append(
                    build.add(
                        action="update_contract",
                        target=f"contract {contract.id} {contract.name!r}",
                        method="PUT",
                        path=f"/contracts/{contract.id}",
                        body={"vendor_id": target.id},
                        response_key="contract",
                        before={"vendor": names.get(contract.vendor_id)},
                        after={"vendor": target.name},
                    )
                )

    try:
        orders = client.get_list("/purchase_orders", "purchase_orders")
    except (FreshServiceAuthError, FreshServiceNotFoundError):
        orders = []
    for order in orders:
        if order.get("vendor_id") in source_ids:
            build.warn(
                f"Purchase order {order.get('po_number') or order.get('id')} references vendor "
                f"{names.get(order['vendor_id'])!r}; purchase orders are not changed"
            )

    if delete:
        for vendor in source_vendors:
            build.add(
                action="delete_vendor",
                target=f"vendor {vendor.id} {vendor.name!r}",
                method="DELETE",
                path=f"/vendors/{vendor.id}",
                requires=depends[vendor.id],
            )
    build.plan.summary = {
        "target": {"id": target.id, "name": target.name},
        "sources": [{"id": vendor.id, "name": vendor.name} for vendor in source_vendors],
        "software": len(software),
        "assets": asset_count if include_assets else "not checked",
        "contracts": contract_count if include_contracts else "not checked",
        "delete_sources": delete,
    }
    return build.plan


# -- software merge -------------------------------------------------------------------------


def plan_software_merge(
    client: FreshServiceClient,
    vendor: str | int,
    sources: Sequence[str | int],
    into: str | int,
    *,
    delete: bool = False,
) -> MaintenancePlan:
    """Fold software entries of ONE vendor into one: e.g. "Google Chrome 120" and "Google
    Chrome 121" into "Google Chrome" (created under the vendor if it doesn't exist).

    Installations and users move to the target (a device on several sources keeps the
    highest version); each source is then marked status=ignored, or deleted with delete.
    Sources with licenses are left alone: the API cannot move licenses.
    """
    vendors = list_vendors(client)
    owner = _one_vendor(vendors, vendor)
    entries = list_software(client, vendor_ids=[owner.id], vendors=vendors)
    by_id = {entry.id: entry for entry in entries}
    build = _Builder("software-merge")
    build.warn(REDISCOVERY_WARNING)

    into_text = str(into).strip()
    target: Software | None = by_id.get(int(into_text)) if into_text.isdigit() else None
    if target is None and into_text.isdigit():
        raise ValueError(f"Software {into_text} is not a {owner.name!r} entry")
    if target is None:
        named = [entry for entry in entries if name_matches(entry.name, into_text)]
        if len(named) > 1:
            raise ValueError(f"Several {owner.name!r} entries are named {into_text!r}; use the id")
        target = named[0] if named else None

    chosen: dict[int, Software] = {}
    for token in sources:
        text = str(token).strip()
        if text.isdigit():
            if int(text) not in by_id:
                raise ValueError(
                    f"Software {text} is not a {owner.name!r} entry; merges stay within one vendor"
                )
            chosen[int(text)] = by_id[int(text)]
            continue
        matches = [entry for entry in entries if name_matches(entry.name, text)]
        if not matches:
            raise ValueError(f"No {owner.name!r} software matches {text!r}")
        chosen.update({entry.id: entry for entry in matches})
    if target is not None:
        chosen.pop(target.id, None)
    if not chosen:
        raise ValueError("No source software left once the target is excluded")

    def usage(entry: Software) -> tuple[Software, list[Installation], list[SoftwareUser], int]:
        return (
            entry,
            list_installations(client, entry.id),
            list_software_users(client, entry.id),
            len(list_licenses(client, entry.id)),
        )

    sources_usage = client.parallel(
        usage, sorted(chosen.values(), key=lambda e: e.name.casefold()), label="usage"
    )
    movable = []
    for entry, installs, users, licenses in sources_usage:
        if licenses:
            build.warn(
                f"{entry.name!r} ({entry.id}) has {licenses} license(s), which can't be moved; left alone"
            )
        else:
            movable.append((entry, installs, users))
    if not movable:
        raise ValueError("Every source has licenses; nothing can be merged")

    if target is None:
        template = max(movable, key=lambda item: len(item[1]))[0]
        build.add(
            action="create_software",
            target=f"software {into_text!r} ({owner.name})",
            method="POST",
            path="/applications",
            body={
                key: value
                for key, value in {
                    "name": into_text,
                    "publisher_id": owner.id,
                    "application_type": template.application_type or "desktop",
                    "category": template.category,
                    "workspace_id": client.settings.workspace_id,
                }.items()
                if value is not None
            },
            response_key="application",
            ref="software:target",
        )
        target_ref = ref("software:target")
        target_machines: set[int] = set()
        target_users: set[int] = set()
    else:
        target_ref = str(target.id)
        target_machines = {
            install.installation_machine_id for install in list_installations(client, target.id)
        }
        target_users = {user.user_id for user in list_software_users(client, target.id)}

    best: dict[int, tuple[Software, Installation]] = {}
    for entry, installs, _users in movable:
        for install in installs:
            machine = install.installation_machine_id
            if machine in target_machines:
                continue
            current = best.get(machine)
            if current is None or version_key(install.version) > version_key(current[1].version):
                best[machine] = (entry, install)

    posts = [
        build.add(
            action="add_installation",
            target=f"device {machine} {install.version or ''} (from {entry.name!r})".replace("  ", " "),
            method="POST",
            path=f"/applications/{target_ref}/installations",
            body={
                key: value
                for key, value in {
                    "installation_machine_id": machine,
                    "installation_path": install.installation_path,
                    "version": install.version,
                    "installation_date": install.installation_date.isoformat()
                    if install.installation_date
                    else None,
                }.items()
                if value is not None
            },
            response_key="installation",
        )
        for machine, (entry, install) in sorted(best.items())
    ]

    users_added: set[int] = set(target_users)
    moved_users = 0
    for entry, installs, users in movable:
        cleanup: list[int] = []
        machines = sorted({install.installation_machine_id for install in installs})
        for start in range(0, len(machines), ID_CHUNK):
            chunk = machines[start : start + ID_CHUNK]
            cleanup.append(
                build.add(
                    action="remove_installations",
                    target=f"{len(chunk)} installation(s) of {entry.name!r} ({entry.id})",
                    method="DELETE",
                    path=f"/applications/{entry.id}/installations",
                    params={"device_ids": ",".join(map(str, chunk))},
                    requires=posts,
                )
            )
        new_users = [user for user in users if user.user_id not in users_added]
        users_added |= {user.user_id for user in new_users}
        moved_users += len(new_users)
        requires_users: list[int] = []
        if new_users:
            requires_users.append(
                build.add(
                    action="add_software_users",
                    target=f"{len(new_users)} user(s) from {entry.name!r}",
                    method="POST",
                    path=f"/applications/{target_ref}/users",
                    body={
                        "application_users": [
                            {
                                key: value
                                for key, value in {
                                    "user_id": user.user_id,
                                    "first_used": user.first_used.isoformat() if user.first_used else None,
                                    "last_used": user.last_used.isoformat() if user.last_used else None,
                                    "source": user.source,
                                }.items()
                                if value is not None
                            }
                            for user in new_users
                        ]
                    },
                )
            )
        user_ids = sorted({user.user_id for user in users})
        for start in range(0, len(user_ids), ID_CHUNK):
            chunk = user_ids[start : start + ID_CHUNK]
            cleanup.append(
                build.add(
                    action="remove_software_users",
                    target=f"{len(chunk)} user(s) of {entry.name!r} ({entry.id})",
                    method="DELETE",
                    path=f"/applications/{entry.id}/users",
                    params={"user_ids": ",".join(map(str, chunk))},
                    requires=requires_users,
                )
            )
        if delete:
            build.add(
                action="delete_software",
                target=f"software {entry.id} {entry.name!r}",
                method="DELETE",
                path=f"/applications/{entry.id}",
                requires=cleanup,
            )
        else:
            build.add(
                action="ignore_software",
                target=f"software {entry.id} {entry.name!r}",
                method="PUT",
                path=f"/applications/{entry.id}",
                body={"status": "ignored"},
                response_key="application",
                requires=cleanup,
                before={"status": entry.status},
                after={"status": "ignored"},
            )

    build.plan.summary = {
        "vendor": {"id": owner.id, "name": owner.name},
        "target": {"id": target.id, "name": target.name}
        if target
        else {"id": None, "name": into_text, "new": True},
        "sources": [
            {"id": entry.id, "name": entry.name, "installations": len(installs), "users": len(users)}
            for entry, installs, users in movable
        ],
        "installations_to_add": len(posts),
        "users_to_add": moved_users,
        "sources_become": "deleted" if delete else "ignored",
    }
    return build.plan


# -- software delete ----------------------------------------------------------------------


@dataclass(frozen=True)
class SoftwareUse:
    """How much one software entry is in use."""

    entry: Software
    installations: int
    users: int
    licenses: int | None
    """None: not checked, because the entry has installations or users anyway."""

    @property
    def unused(self) -> bool:
        return not (self.installations or self.users or self.licenses)


def software_use(client: FreshServiceClient, entries: Iterable[Software]) -> list[SoftwareUse]:
    """Installations, users and licenses per entry. The listing's counts are used where the API
    gives them; licenses take one call per entry, and only for entries with no installations
    and no users (the ones that could be unused)."""

    def use(entry: Software) -> SoftwareUse:
        installs = entry.installation_count
        if installs is None:
            installs = len(list_installations(client, entry.id))
        users = entry.user_count
        if users is None:
            users = len(list_software_users(client, entry.id))
        licenses = len(list_licenses(client, entry.id)) if not (installs or users) else None
        return SoftwareUse(entry, installs, users, licenses)

    return client.parallel(use, list(entries), label="usage")


def _delete_software_action(build: _Builder, use: SoftwareUse) -> int:
    entry = use.entry
    return build.add(
        action="delete_software",
        target=f"software {entry.id} {entry.name!r} ({entry.publisher_name or 'no publisher'})",
        method="DELETE",
        path=f"/applications/{entry.id}",
        before={
            "installations": use.installations,
            "users": use.users,
            "licenses": use.licenses,
            "status": entry.status,
        },
    )


def plan_software_delete(
    client: FreshServiceClient,
    *,
    vendor: str | None = None,
    name: str | None = None,
    ids: Sequence[int] = (),
    status: str | None = None,
    include_used: bool = False,
) -> MaintenancePlan:
    """Delete software entries (permanently). By default only unused ones: no installations,
    no users and no licenses. include_used deletes the selected entries regardless."""
    if not (vendor or name or ids or status):
        raise ValueError("Select software: vendor, name, ids or status")
    entries = list_software(client, vendor=vendor, name=name, status=status)
    if ids:
        wanted = set(ids)
        entries = [entry for entry in entries if entry.id in wanted]
    build = _Builder("software-delete")
    uses = (
        software_use(client, entries)
        if not include_used
        else [SoftwareUse(e, e.installation_count or 0, e.user_count or 0, None) for e in entries]
    )
    kept = 0
    for use in sorted(uses, key=lambda u: u.entry.name.casefold()):
        if not include_used and not use.unused:
            kept += 1
            continue
        _delete_software_action(build, use)
    build.plan.summary = {
        "selected": len(entries),
        "to_delete": len(build.plan.actions),
        "kept_in_use": kept,
        "include_used": include_used,
    }
    return build.plan


# -- software and publisher cleanup -------------------------------------------------------


def vendor_references(client: FreshServiceClient, vendor_ids: Iterable[int]) -> dict[int, list[str]]:
    """What still points at each vendor besides software: assets (vendor field), contracts and
    purchase orders. Raises FreshServiceError if one of them can't be read, so a caller never
    mistakes "couldn't check" for "not referenced"."""
    wanted = set(vendor_ids)
    found: dict[int, list[str]] = defaultdict(list)
    if not wanted:
        return found
    for asset in list_assets(client, include_type_fields=True):
        if asset.vendor_id in wanted:
            found[asset.vendor_id].append(f"asset {asset.display_id} {asset.name!r}")
    for contract in list_contracts(client):
        if contract.vendor_id in wanted:
            found[contract.vendor_id].append(f"contract {contract.id} {contract.name!r}")
    try:
        orders = client.get_list("/purchase_orders", "purchase_orders")
    except FreshServiceNotFoundError:  # accounts without purchase orders
        orders = []
    for order in orders:
        vendor_id = order.get("vendor_id")
        if vendor_id in wanted:
            found[vendor_id].append(f"purchase order {order.get('po_number') or order.get('id')}")
    return found


def plan_software_cleanup(
    client: FreshServiceClient,
    *,
    publisher: str | int | None = None,
    include_unregistered: bool = True,
    delete_publishers: bool = True,
    orphan_vendors: bool = False,
) -> MaintenancePlan:
    """Wipe unused software, then the publishers left without software.

    1. Group all software by publisher; entries with no publisher, or one that no longer
       exists, are "unregistered" (included unless include_unregistered=False or a
       publisher is given).
    2. Delete every entry with no installations, no users and no licenses.
    3. Delete each publisher (vendor) whose software is now all gone - unless assets,
       contracts or purchase orders still point at it (reported, kept). If those can't be
       read, no vendor is deleted.

    orphan_vendors also deletes vendors with no software and no references at all, e.g.
    publishers emptied by an earlier run whose vendor delete didn't happen.
    """
    vendors = list_vendors(client)
    by_id = {vendor.id: vendor for vendor in vendors}
    entries = list_software(client, vendors=vendors)
    software_of: dict[int, list[Software]] = defaultdict(list)
    unregistered: list[Software] = []
    for entry in entries:
        if entry.publisher_id in by_id:
            software_of[entry.publisher_id].append(entry)
        else:
            unregistered.append(entry)

    if publisher is not None:
        selected = publisher_ids(vendors, publisher) & software_of.keys()
        if not selected:
            raise ValueError(f"No software publisher matches {publisher!r}")
        include_unregistered = False
    else:
        selected = set(software_of)
    scope = [entry for vendor_id in selected for entry in software_of[vendor_id]]
    if include_unregistered:
        scope += unregistered

    uses = {use.entry.id: use for use in software_use(client, scope)}
    build = _Builder("software-cleanup")
    deleted_per_vendor: dict[int, list[int]] = defaultdict(list)
    kept_in_use = 0
    for use in sorted(
        uses.values(), key=lambda u: ((u.entry.publisher_name or "~").casefold(), u.entry.name.casefold())
    ):
        if not use.unused:
            kept_in_use += 1
            continue
        index = _delete_software_action(build, use)
        if use.entry.publisher_id in selected:
            deleted_per_vendor[use.entry.publisher_id].append(index)

    emptied = [
        vendor_id
        for vendor_id in selected
        if delete_publishers and len(deleted_per_vendor[vendor_id]) == len(software_of[vendor_id])
    ]
    orphans = (
        [v.id for v in vendors if v.id not in software_of]
        if orphan_vendors and publisher is None and delete_publishers
        else []
    )
    candidates = emptied + orphans
    kept_referenced: dict[int, list[str]] = {}
    if candidates:
        try:
            references = vendor_references(client, candidates)
        except FreshServiceError as exc:
            build.warn(f"Could not check what references the vendors ({exc}); no vendor is deleted")
            candidates, references = [], {}
        for vendor_id in sorted(candidates, key=lambda i: by_id[i].name.casefold()):
            refs = references.get(vendor_id)
            if refs:
                kept_referenced[vendor_id] = refs
                more = f" and {len(refs) - 3} more" if len(refs) > 3 else ""
                name = by_id[vendor_id].name
                build.warn(f"Vendor {name!r} has no software left but is kept: {', '.join(refs[:3])}{more}")
                continue
            build.add(
                action="delete_vendor",
                target=f"vendor {vendor_id} {by_id[vendor_id].name!r}"
                + (
                    " (no software left)" if vendor_id in emptied else " (orphan: no software, no references)"
                ),
                method="DELETE",
                path=f"/vendors/{vendor_id}",
                requires=deleted_per_vendor.get(vendor_id, []),
            )

    actions = build.plan.actions
    build.plan.summary = {
        "publishers_scanned": len(selected),
        "software_scanned": len(scope),
        "unregistered_scanned": len(unregistered) if include_unregistered else "not included",
        "software_to_delete": sum(1 for a in actions if a.action == "delete_software"),
        "software_kept_in_use": kept_in_use,
        "publishers_to_delete": sum(1 for a in actions if a.action == "delete_vendor"),
        "publishers_kept_referenced": [
            {"id": vendor_id, "name": by_id[vendor_id].name, "references": len(refs)}
            for vendor_id, refs in kept_referenced.items()
        ],
    }
    return build.plan
