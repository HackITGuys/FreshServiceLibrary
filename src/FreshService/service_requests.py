"""Service requests: the service catalog and placing requests for its items.

A service request is a ticket with type "Service Request", so it is read, updated and
noted on with the ticket functions (get_ticket, update_ticket, add_note).

ATTACHMENTS. place_request only takes files in the catalog item's own attachment fields
(custom_fields[<field>][]). Free attachments are added as a private note on the new
request, the closest the API allows.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from FreshService._util import drop_none, workspace
from FreshService.client import Attachments, FreshServiceClient
from FreshService.models import CatalogCategory, CatalogItem, RequestedItem, Ticket
from FreshService.tickets import add_note

CATALOG_PAGE_SIZE = 30
"""The service catalog endpoints serve at most 30 per page."""


def list_catalog_categories(client: FreshServiceClient) -> list[CatalogCategory]:
    return [
        CatalogCategory.model_validate(raw)
        for raw in client.paginate("/service_catalog/categories", "service_categories")
    ]


def list_catalog_items(
    client: FreshServiceClient, *, category_id: int | None = None, workspace_id: int | None = None
) -> list[CatalogItem]:
    params = drop_none({"category_id": category_id, "workspace_id": workspace(client, workspace_id)})
    return [
        CatalogItem.model_validate(raw)
        for raw in client.paginate(
            "/service_catalog/items", "service_items", params, page_size=CATALOG_PAGE_SIZE
        )
    ]


def search_catalog_items(client: FreshServiceClient, search_term: str) -> list[CatalogItem]:
    return [
        CatalogItem.model_validate(raw)
        for raw in client.paginate(
            "/service_catalog/items/search",
            "service_items",
            {"search_term": search_term},
            page_size=CATALOG_PAGE_SIZE,
        )
    ]


def get_catalog_item(client: FreshServiceClient, display_id: int) -> CatalogItem:
    """One catalog item, with its form fields (custom_fields) for place_request."""
    return CatalogItem.model_validate(
        client.get_object(f"/service_catalog/items/{display_id}", "service_item")
    )


def create_service_request(
    client: FreshServiceClient,
    item_display_id: int,
    *,
    quantity: int = 1,
    email: str | None = None,
    requested_for: str | None = None,
    custom_fields: Mapping[str, Any] | None = None,
    child_items: Sequence[Mapping[str, Any]] | None = None,
    parent_ticket_id: int | None = None,
    workspace_id: int | None = None,
    attachments: Attachments = (),
    attachment_note: str = "<p>Attachments for this request.</p>",
) -> Ticket:
    """Request a catalog item. email: the requester (default: the API key's agent);
    requested_for: whom it is for. custom_fields: the item's form fields, by name.

    Files in `attachments` are added afterwards as a private note on the request.
    """
    body = drop_none(
        {
            "quantity": quantity,
            "email": email,
            "requested_for": requested_for,
            "custom_fields": dict(custom_fields) if custom_fields else None,
            "child_items": [dict(item) for item in child_items] if child_items else None,
            "parent_ticket_id": parent_ticket_id,
            "workspace_id": workspace(client, workspace_id),
        }
    )
    raw = client.post(
        f"/service_catalog/items/{item_display_id}/place_request", body, key=("service_request", "ticket")
    )
    request = Ticket.model_validate(raw)
    files = list(attachments)
    if files:
        add_note(client, request.id, attachment_note, attachments=files)
    return request


def list_requested_items(client: FreshServiceClient, ticket_id: int) -> list[RequestedItem]:
    """The catalog items requested on a service request."""
    return [
        RequestedItem.model_validate(raw)
        for raw in client.get_list(f"/tickets/{ticket_id}/requested_items", "requested_items")
    ]


def update_requested_item(
    client: FreshServiceClient, ticket_id: int, requested_item_id: int, **fields: Any
) -> RequestedItem:
    """quantity, stage (RequestedItemStage) or custom_fields of one requested item."""
    return RequestedItem.model_validate(
        client.put(
            f"/tickets/{ticket_id}/requested_items/{requested_item_id}",
            drop_none(fields),
            key="requested_item",
        )
    )
