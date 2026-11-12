"""Shared fixtures: a client against a mocked Freshservice API (no network).

The mock fails any request that isn't registered, so a test that registers only GETs
also proves that nothing was written.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

import pytest
import responses
from responses import matchers

from FreshService import FreshServiceClient, FreshServiceSettings

API = "https://fs.test/api/v2"
PAGE_SIZE = 2
"""Matches the settings fixture."""


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never pick up a developer's real FRESH_* environment during tests."""
    import os

    for name in list(os.environ):
        if name.startswith("FRESH_"):
            monkeypatch.delenv(name)


@pytest.fixture
def settings() -> FreshServiceSettings:
    # _env_file=None: never read a developer's real .env during tests.
    return FreshServiceSettings(
        _env_file=None,
        domain="fs.test",
        api_key="test-key",
        page_size=PAGE_SIZE,
        max_attempts=3,
        max_workers=2,
    )


@pytest.fixture
def api() -> Iterator[responses.RequestsMock]:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        yield mock


@pytest.fixture
def client(settings: FreshServiceSettings, api: responses.RequestsMock) -> FreshServiceClient:
    return FreshServiceClient(settings)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr("FreshService.client.time.sleep", slept.append)
    return slept


def get(
    mock: responses.RequestsMock, path: str, body: Any, params: dict[str, Any] | None = None, **kwargs: Any
) -> None:
    """Register GET {path}; with params, only a request with exactly these query params matches."""
    match = (
        [matchers.query_param_matcher({k: str(v) for k, v in params.items()})] if params is not None else []
    )
    mock.get(API + path, json=body, match=match, **kwargs)


def pages(
    mock: responses.RequestsMock,
    path: str,
    key: str,
    rows: Sequence[dict[str, Any]],
    params: dict[str, Any] | None = None,
    *,
    page_size: int = PAGE_SIZE,
) -> None:
    """Register a paged list endpoint the way Freshservice serves it: `link: rel="next"` on
    every page but the last."""
    chunks = [list(rows[i : i + page_size]) for i in range(0, len(rows), page_size)] or [[]]
    if len(chunks[-1]) == page_size:
        chunks.append([])  # a full last page without a link header makes the client ask once more
    for number, chunk in enumerate(chunks, start=1):
        last = number == len(chunks)
        headers = {} if last else {"link": f'<{API}{path}?page={number + 1}>; rel="next"'}
        query = {**(params or {}), "page": number, "per_page": page_size}
        get(mock, path, {key: chunk}, query, headers=headers)


def query_pages(
    mock: responses.RequestsMock,
    path: str,
    key: str,
    rows: Sequence[dict[str, Any]],
    params: dict[str, Any],
) -> None:
    """Register a filter/search endpoint: 30 per page, `page` only."""
    chunks = [list(rows[i : i + 30]) for i in range(0, len(rows), 30)] or [[]]
    if len(chunks[-1]) == 30:
        chunks.append([])
    for number, chunk in enumerate(chunks, start=1):
        last = number == len(chunks)
        headers = {} if last else {"link": f'<{API}{path}?page={number + 1}>; rel="next"'}
        get(mock, path, {key: chunk}, {**params, "page": number}, headers=headers)


def writes(mock: responses.RequestsMock) -> list[tuple[str, str]]:
    """(method, url) of every non-GET call made."""
    return [
        (call.request.method or "", call.request.url or "")
        for call in mock.calls
        if call.request.method != "GET"
    ]


# -- sample records ------------------------------------------------------------------

ASSET_TYPES = [
    {"id": 1, "name": "Hardware", "parent_asset_type_id": None},
    {"id": 2, "name": "Computer", "parent_asset_type_id": 1},
    {"id": 3, "name": "Network", "parent_asset_type_id": 1},
    {"id": 4, "name": "Firewall", "parent_asset_type_id": 3},
    {"id": 5, "name": "Switch", "parent_asset_type_id": 3},
    {"id": 9, "name": "Services", "parent_asset_type_id": None},
]


def hardware_fields(type_id: int) -> dict[str, Any]:
    """/asset_types/{type_id}/fields for a type under Hardware (fields defined on type 1)."""
    return {
        "asset_type_fields": [
            {"field_header": "General", "fields": [{"id": None, "name": "name", "label": "Display Name"}]},
            {
                "field_header": "Hardware",
                "fields": [
                    {"id": 11, "name": "product_1", "label": "Product", "asset_type_id": 1},
                    {"id": 12, "name": "vendor_1", "label": "Vendor", "asset_type_id": 1},
                    {"id": 13, "name": "asset_state_1", "label": "Asset State", "asset_type_id": 1},
                    {"id": 14, "name": "serial_number_1", "label": "Serial Number", "asset_type_id": 1},
                ],
            },
        ]
    }


def asset(
    display_id: int,
    name: str,
    type_id: int,
    *,
    product: int | None = None,
    vendor: int | None = None,
    state: str | None = "In Use",
) -> dict[str, Any]:
    return {
        "id": display_id + 1000,
        "display_id": display_id,
        "name": name,
        "asset_type_id": type_id,
        "type_fields": {"product_1": product, "vendor_1": vendor, "asset_state_1": state},
    }


def register_asset_types(mock: responses.RequestsMock, *type_ids: int) -> None:
    pages(mock, "/asset_types", "asset_types", ASSET_TYPES)
    for type_id in type_ids:
        get(mock, f"/asset_types/{type_id}/fields", hardware_fields(type_id))
