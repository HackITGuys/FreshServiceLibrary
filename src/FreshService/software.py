"""Software (/applications): entries, their installations, users and licenses, and the
fleet-wide "where is it installed" index.

The classic API has no account-wide installations listing and no "software on this
asset" endpoint. where_installed() builds both from one installations call per matching
software entry (in parallel, FRESH_MAX_WORKERS at a time), then joins the asset of each
installation (installation_machine_id is the asset's display_id).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from FreshService._util import drop_none, workspace
from FreshService.assets import asset_type_tree, asset_url, assets_by_display_id
from FreshService.catalog import list_vendors
from FreshService.client import FreshServiceAuthError, FreshServiceClient
from FreshService.matching import text_matches, version_key, version_matches, version_specifier
from FreshService.models import (
    Asset,
    DeviceInstall,
    DeviceSoftware,
    Installation,
    InstallSummary,
    Software,
    SoftwareLicense,
    SoftwareUser,
    Vendor,
)

log = logging.getLogger("FreshService")

ID_CHUNK = 50
"""Ids per bulk-delete call (they go in the query string)."""
SOFTWARE_KEYS = ("applications", "application")
"""The list envelope is "applications" (live) or "application" (the docs' sample)."""


def _chunks(values: Sequence[int], size: int = ID_CHUNK) -> list[list[int]]:
    return [list(values[i : i + size]) for i in range(0, len(values), size)]


# -- software entries ----------------------------------------------------------------


def list_software(
    client: FreshServiceClient,
    *,
    vendor: str | None = None,
    name: str | None = None,
    vendor_ids: Iterable[int] | None = None,
    status: str | None = None,
    vendors: Iterable[Vendor] | None = None,
    workspace_id: int | None = None,
) -> list[Software]:
    """Software entries, filtered by vendor name (substring/glob), vendor ids, name and status.

    Pass `vendors` (from list_vendors) to avoid fetching them again for the vendor filter.
    status "none" selects entries with no status (not yet triaged).
    """
    params = drop_none({"workspace_id": workspace(client, workspace_id)})
    entries = [
        Software.model_validate(raw) for raw in client.paginate("/applications", SOFTWARE_KEYS, params)
    ]
    ids: set[int] | None = set(vendor_ids) if vendor_ids is not None else None
    if vendor:
        pool = list(vendors) if vendors is not None else list_vendors(client)
        matched = {item.id for item in pool if text_matches(item.name, vendor)}
        ids = matched if ids is None else ids & matched

    def status_ok(entry: Software) -> bool:
        if status is None:
            return True
        if status == "none":
            return not entry.status
        return (entry.status or "").casefold() == status.casefold()

    return [
        entry
        for entry in entries
        if (ids is None or entry.publisher_id in ids) and text_matches(entry.name, name) and status_ok(entry)
    ]


def get_software(client: FreshServiceClient, software_id: int) -> Software:
    return Software.model_validate(client.get_object(f"/applications/{software_id}", "application"))


def create_software(
    client: FreshServiceClient,
    *,
    name: str,
    publisher_id: int | None = None,
    application_type: str = "desktop",
    **fields: Any,
) -> Software:
    """fields: status (managed/ignored/blacklisted), category, description, notes, managed_by_id."""
    body = drop_none(
        {
            "name": name,
            "publisher_id": publisher_id,
            "application_type": application_type,
            "workspace_id": client.settings.workspace_id,
            **fields,
        }
    )
    return Software.model_validate(client.post("/applications", body, key="application"))


def update_software(client: FreshServiceClient, software_id: int, **fields: Any) -> Software:
    """Change name, publisher_id (vendor), status, category, ..."""
    return Software.model_validate(
        client.put(f"/applications/{software_id}", drop_none(fields), key="application")
    )


def delete_software(client: FreshServiceClient, software_id: int) -> None:
    """Delete a software entry permanently (there is no restore)."""
    client.delete(f"/applications/{software_id}")


# -- installations, users, licenses ----------------------------------------------


def list_installations(client: FreshServiceClient, software_id: int) -> list[Installation]:
    return [
        Installation.model_validate(raw)
        for raw in client.paginate(f"/applications/{software_id}/installations", "installations")
    ]


def add_installation(
    client: FreshServiceClient,
    software_id: int,
    device_id: int,
    *,
    path: str | None = None,
    version: str | None = None,
    installation_date: str | None = None,
) -> Installation:
    """Record the software as installed on a device (the asset's display_id)."""
    body = drop_none(
        {
            "installation_machine_id": device_id,
            "installation_path": path,
            "version": version,
            "installation_date": installation_date,
        }
    )
    return Installation.model_validate(
        client.post(f"/applications/{software_id}/installations", body, key="installation")
    )


def remove_installations(client: FreshServiceClient, software_id: int, device_ids: Iterable[int]) -> None:
    for chunk in _chunks(sorted(set(device_ids))):
        client.delete(f"/applications/{software_id}/installations", {"device_ids": ",".join(map(str, chunk))})


def list_software_users(client: FreshServiceClient, software_id: int) -> list[SoftwareUser]:
    return [
        SoftwareUser.model_validate(raw)
        for raw in client.paginate(f"/applications/{software_id}/users", "application_users")
    ]


def add_software_users(
    client: FreshServiceClient, software_id: int, users: Iterable[Mapping[str, Any]]
) -> list[SoftwareUser]:
    """users: [{user_id, license_id?, allocated_date?, first_used?, last_used?, source?}]."""
    payload = {"application_users": [drop_none(dict(user)) for user in users]}
    raws = client.post(f"/applications/{software_id}/users", payload, key="application_users")
    return [SoftwareUser.model_validate(raw) for raw in raws or []]


def remove_software_users(client: FreshServiceClient, software_id: int, user_ids: Iterable[int]) -> None:
    for chunk in _chunks(sorted(set(user_ids))):
        client.delete(f"/applications/{software_id}/users", {"user_ids": ",".join(map(str, chunk))})


def list_licenses(client: FreshServiceClient, software_id: int) -> list[SoftwareLicense]:
    return [
        SoftwareLicense.model_validate(raw)
        for raw in client.paginate(f"/applications/{software_id}/licenses", "licenses")
    ]


# -- where is it installed -----------------------------------------------------------


def where_installed(
    client: FreshServiceClient,
    *,
    vendor: str | None = None,
    name: str | None = None,
    version: str | None = None,
    software: Iterable[Software] | None = None,
    vendors: Iterable[Vendor] | None = None,
    assets: Mapping[int, Asset] | None = None,
) -> list[DeviceInstall]:
    """Every installation of the matching software, joined with its vendor and device.

    vendor and name are substrings or globs; version is exact, a glob or a PEP 440 range
    ("<24.09"). Pass `software` to skip the software listing, `assets` (by display_id) to
    skip fetching devices you already have.
    """
    if version:
        version_specifier(version)  # fail on a bad specifier before any API call
    vendor_list = list(vendors) if vendors is not None else list_vendors(client)
    vendor_names = {item.id: item.name for item in vendor_list}
    entries = (
        list(software)
        if software is not None
        else list_software(client, vendor=vendor, name=name, vendors=vendor_list)
    )
    entries = [entry for entry in entries if entry.installation_count != 0]

    per_entry = client.parallel(lambda entry: (entry, list_installations(client, entry.id)), entries)
    pairs = [
        (entry, install)
        for entry, installs in per_entry
        for install in installs
        if version_matches(install.version, version)
    ]
    devices = assets_by_display_id(
        client, {install.installation_machine_id for _, install in pairs}, known=assets
    )
    tree = None
    if devices:
        try:
            tree = asset_type_tree(client)
        except FreshServiceAuthError:  # a role without asset-type access: names stay empty
            log.info("no access to /asset_types; asset_type_name is left empty")

    results = []
    for entry, install in pairs:
        device = devices.get(install.installation_machine_id)
        type_id = device.asset_type_id if device else None
        results.append(
            DeviceInstall(
                software_id=entry.id,
                software_name=entry.name,
                vendor_id=entry.publisher_id,
                vendor_name=vendor_names.get(entry.publisher_id) if entry.publisher_id else None,
                version=install.version,
                installation_path=install.installation_path,
                installation_date=install.installation_date,
                device_id=install.installation_machine_id,
                device_name=device.name if device else None,
                device_state=device.asset_state if device else None,
                asset_type_id=type_id,
                asset_type_name=tree.name(type_id) if tree else None,
                user_id=install.user_id
                if install.user_id is not None
                else (device.user_id if device else None),
                department_id=install.department_id,
                url=asset_url(client, install.installation_machine_id),
            )
        )
    return sorted(results, key=lambda r: (r.software_name.casefold(), (r.device_name or "").casefold()))


def summarise_installs(installs: Iterable[DeviceInstall]) -> list[InstallSummary]:
    """Per software: device count, and devices per version (newest first). Most widespread first."""
    devices: dict[int, set[int]] = defaultdict(set)
    versions: dict[int, dict[str, set[int]]] = defaultdict(lambda: defaultdict(set))
    first: dict[int, DeviceInstall] = {}
    for install in installs:
        first.setdefault(install.software_id, install)
        devices[install.software_id].add(install.device_id)
        versions[install.software_id][install.version or ""].add(install.device_id)
    summaries = [
        InstallSummary(
            software_id=software_id,
            software_name=first[software_id].software_name,
            vendor_name=first[software_id].vendor_name,
            device_count=len(devices[software_id]),
            versions={
                version: len(ids)
                for version, ids in sorted(
                    versions[software_id].items(), key=lambda item: version_key(item[0]), reverse=True
                )
            },
        )
        for software_id in devices
    ]
    return sorted(summaries, key=lambda s: (-s.device_count, s.software_name.casefold()))


def by_device(installs: Iterable[DeviceInstall]) -> list[DeviceSoftware]:
    """The same installs grouped per device."""
    grouped: dict[int, DeviceSoftware] = {}
    for install in installs:
        device = grouped.setdefault(
            install.device_id,
            DeviceSoftware(
                device_id=install.device_id,
                device_name=install.device_name,
                device_state=install.device_state,
                asset_type_name=install.asset_type_name,
                url=install.url,
            ),
        )
        device.software.append(
            {
                "software_id": install.software_id,
                "software_name": install.software_name,
                "vendor_name": install.vendor_name,
                "version": install.version,
            }
        )
    return sorted(grouped.values(), key=lambda d: (d.device_name or "").casefold())
