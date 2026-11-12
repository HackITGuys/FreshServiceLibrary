from __future__ import annotations

import json
from pathlib import Path

import pytest
import responses

from FreshService import (
    FreshServiceAuthError,
    FreshServiceClient,
    FreshServiceConfigError,
    FreshServiceError,
    FreshServiceNotFoundError,
    FreshServiceSettings,
    FreshServiceTruncatedError,
    form_fields,
    load_attachments,
)
from tests.conftest import API, get, pages, query_pages


def test_paginate_follows_the_link_header(client: FreshServiceClient, api: responses.RequestsMock) -> None:
    pages(api, "/vendors", "vendors", [{"id": 1}, {"id": 2}, {"id": 3}])
    assert [row["id"] for row in client.paginate("/vendors", "vendors")] == [1, 2, 3]
    assert len(api.calls) == 2


def test_paginate_without_link_headers_falls_back_to_page_length(
    client: FreshServiceClient, api: responses.RequestsMock
) -> None:
    get(api, "/roles", {"roles": [{"id": 1}, {"id": 2}]}, {"page": 1, "per_page": 2})
    get(api, "/roles", {"roles": []}, {"page": 2, "per_page": 2})
    assert [row["id"] for row in client.paginate("/roles", "roles")] == [1, 2]


def test_a_renamed_envelope_fails_loudly(client: FreshServiceClient, api: responses.RequestsMock) -> None:
    get(api, "/roles", {"agent_roles": []})
    with pytest.raises(FreshServiceError, match="'roles'"):
        list(client.paginate("/roles", "roles"))


def test_a_repeated_page_is_an_error(client: FreshServiceClient, api: responses.RequestsMock) -> None:
    same = {"roles": [{"id": 1}, {"id": 2}]}
    get(api, "/roles", same, {"page": 1, "per_page": 2})
    get(api, "/roles", same, {"page": 2, "per_page": 2})
    with pytest.raises(FreshServiceError, match="identical"):
        list(client.paginate("/roles", "roles"))


def test_429_honours_retry_after(
    client: FreshServiceClient, api: responses.RequestsMock, no_sleep: list[float]
) -> None:
    api.get(API + "/roles", status=429, headers={"Retry-After": "7"})
    api.get(API + "/roles", json={"roles": []})
    assert client.get("/roles") == {"roles": []}
    assert no_sleep == [7.0]


def test_a_post_is_not_retried_on_5xx(client: FreshServiceClient, api: responses.RequestsMock) -> None:
    api.post(API + "/tickets", status=503)
    with pytest.raises(FreshServiceError, match="HTTP 503"):
        client.post("/tickets", {"subject": "x"}, key="ticket")
    assert len(api.calls) == 1


def test_low_rate_limit_pauses(
    client: FreshServiceClient, api: responses.RequestsMock, no_sleep: list[float]
) -> None:
    api.get(API + "/roles", json={"roles": []}, headers={"X-RateLimit-Remaining": "2"})
    client.get("/roles")
    assert no_sleep == [5.0]


def test_validation_errors_name_the_field(client: FreshServiceClient, api: responses.RequestsMock) -> None:
    api.post(
        API + "/tickets",
        status=400,
        json={
            "description": "Validation failed",
            "errors": [
                {"field": "email", "message": "It should be a valid email address", "code": "invalid_value"}
            ],
        },
    )
    with pytest.raises(FreshServiceError) as caught:
        client.post("/tickets", {"email": "nope"}, key="ticket")
    assert "Validation failed - email: It should be a valid email address (invalid_value)" in str(
        caught.value
    )
    assert caught.value.status == 400
    assert caught.value.errors[0]["field"] == "email"


def test_status_specific_errors(client: FreshServiceClient, api: responses.RequestsMock) -> None:
    api.get(API + "/tickets/1", status=404, json={"code": "not_found", "message": "not found"})
    api.get(API + "/tickets/2", status=401)
    api.get(API + "/tickets/3", status=403, json={"code": "access_denied", "message": "denied"})
    with pytest.raises(FreshServiceNotFoundError):
        client.get("/tickets/1")
    with pytest.raises(FreshServiceAuthError, match="FRESH_API_KEY"):
        client.get("/tickets/2")
    with pytest.raises(FreshServiceAuthError, match="permission"):
        client.get("/tickets/3")


def test_basic_auth_with_the_key(client: FreshServiceClient, api: responses.RequestsMock) -> None:
    get(api, "/roles", {"roles": []})
    client.get("/roles")
    # base64("test-key:X")
    assert api.calls[0].request.headers["Authorization"] == "Basic dGVzdC1rZXk6WA=="


def test_query_pagination_raises_when_truncated(
    client: FreshServiceClient, api: responses.RequestsMock
) -> None:
    rows = [{"id": i} for i in range(1, 92)]  # 4 pages of 30 (the last short)
    query_pages(api, "/assets", "assets", rows, {"filter": '"x:1"'})
    assert len(list(client.paginate_query("/assets", "assets", {"filter": '"x:1"'}, max_pages=4))) == 91
    with pytest.raises(FreshServiceTruncatedError, match="more than 60"):
        list(client.paginate_query("/assets", "assets", {"filter": '"x:1"'}, max_pages=2))


def test_form_fields_flatten_like_rails() -> None:
    fields = form_fields(
        {
            "subject": "Hi",
            "priority": 2,
            "private": False,
            "tags": ["a", "b"],
            "custom_fields": {"team": "Ops", "systems": ["x"]},
            "assets": [{"display_id": 7}],
            "group_id": None,
        }
    )
    assert fields == [
        ("subject", "Hi"),
        ("priority", "2"),
        ("private", "false"),
        ("tags[]", "a"),
        ("tags[]", "b"),
        ("custom_fields[team]", "Ops"),
        ("custom_fields[systems][]", "x"),
        ("assets[][display_id]", "7"),
    ]


def test_attachments_are_checked_before_sending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(FreshServiceError, match="not a readable file"):
        load_attachments([tmp_path / "missing.pdf"])
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 11)
    monkeypatch.setattr("FreshService.client.MAX_ATTACHMENT_BYTES", 10)
    with pytest.raises(FreshServiceError, match="40 MB"):
        load_attachments([big])


def test_attachments_go_multipart(
    client: FreshServiceClient, api: responses.RequestsMock, tmp_path: Path
) -> None:
    report = tmp_path / "report.txt"
    report.write_text("hello")
    api.post(API + "/tickets/5/notes", json={"conversation": {"id": 1}})
    client.post(
        "/tickets/5/notes",
        {"body": "<p>x</p>", "notify_emails": ["a@b.c"]},
        key="conversation",
        attachments=[report],
    )
    request = api.calls[0].request
    assert request.headers["Content-Type"].startswith("multipart/form-data")
    body = request.body if isinstance(request.body, bytes) else str(request.body).encode()
    assert b'name="attachments[]"; filename="report.txt"' in body
    assert b'name="notify_emails[]"' in body
    assert b"hello" in body


def test_json_bodies_serialise_enums_and_dates(
    client: FreshServiceClient, api: responses.RequestsMock
) -> None:
    from datetime import date

    from FreshService import Priority

    api.post(API + "/x", json={"x": {}})
    client.post("/x", {"priority": Priority.HIGH, "when": date(2026, 1, 2)}, key="x")
    assert json.loads(api.calls[0].request.body or b"") == {"priority": 3, "when": "2026-01-02"}


def test_audit_log(settings: FreshServiceSettings, api: responses.RequestsMock, tmp_path: Path) -> None:
    log = tmp_path / "audit.jsonl"
    client = FreshServiceClient(settings, audit_log=log)
    api.put(API + "/tickets/1", json={"ticket": {"id": 1}})
    get(api, "/tickets/1", {"ticket": {"id": 1}})
    client.put("/tickets/1", {"priority": 2}, key="ticket")
    client.get("/tickets/1")
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(entries) == 1
    assert (
        entries[0]["method"] == "PUT"
        and entries[0]["body"] == {"priority": 2}
        and entries[0]["status"] == 200
    )


def test_missing_settings_fail_at_client_creation() -> None:
    with pytest.raises(FreshServiceConfigError, match="FRESH_DOMAIN and FRESH_API_KEY"):
        FreshServiceClient(FreshServiceSettings(_env_file=None))


def test_settings_read_legacy_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRESH_DOMAIN", "https://acme.freshservice.com/")
    monkeypatch.setenv("FRESH_KEY", "legacy")
    monkeypatch.setenv("FRESH_DEFAULT_DEPT_ID", "0")
    monkeypatch.setenv("FRESH_TEMPLATE_FILEPATH", "./t.json")
    monkeypatch.setenv("FRESH_DEVICE_ASSET_TYPES", "Hardware, Network")
    settings = FreshServiceSettings(_env_file=None)
    assert settings.domain == "acme.freshservice.com"
    assert settings.api_key.get_secret_value() == "legacy"
    assert settings.default_department_id is None
    assert settings.template_file == Path("t.json")
    assert settings.device_asset_types == ["Hardware", "Network"]
    assert settings.api_root == "https://acme.freshservice.com/api/v2"


def test_settings_by_keyword() -> None:
    settings = FreshServiceSettings(
        _env_file=None, domain="a.freshservice.com", api_key="k", portal_url="https://help.a.no/"
    )
    assert settings.api_key.get_secret_value() == "k"
    assert settings.portal_root == "https://help.a.no"
