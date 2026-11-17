from __future__ import annotations

import io
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
import requests
import responses
from click.testing import CliRunner

from FreshService import (
    FreshServiceClient,
    FreshServiceError,
    FreshServiceSettings,
    disable_debug,
    enable_debug,
)
from FreshService.cli import State, main
from tests.conftest import API, pages


@pytest.fixture(autouse=True)
def reset_logging() -> Iterator[None]:
    yield
    disable_debug()


def test_failed_requests_carry_and_log_the_request_id(
    client: FreshServiceClient, api: responses.RequestsMock, caplog: pytest.LogCaptureFixture
) -> None:
    api.post(
        API + "/tickets",
        status=400,
        headers={"x-request-id": "req-123"},
        json={
            "description": "Validation failed",
            "errors": [{"field": "email", "message": "bad", "code": "invalid_value"}],
        },
    )
    caplog.set_level(logging.DEBUG, logger="FreshService")
    with pytest.raises(FreshServiceError) as caught:
        client.post("/tickets", {"email": "nope", "subject": "x"}, key="ticket")
    assert caught.value.request_id == "req-123" and "[request id req-123]" in str(caught.value)
    assert '"description": "Validation failed"' in (caught.value.body or "")
    messages = [r.getMessage() for r in caplog.records if r.name == "FreshService.http"]
    assert any(m.startswith('POST /tickets body: {"email": "nope", "subject": "x"}') for m in messages)
    assert any("-> 400" in m and "request id req-123" in m for m in messages)
    assert any(
        "failed: HTTP 400, request id req-123, response:" in m and "Validation failed" in m for m in messages
    )
    assert all("test-key" not in r.getMessage() for r in caplog.records)  # the API key never appears


def test_connection_errors_are_retried_for_reads_only(
    client: FreshServiceClient,
    api: responses.RequestsMock,
    no_sleep: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    api.get(API + "/roles", body=requests.ConnectionError("connection reset"))
    api.get(API + "/roles", json={"roles": []})
    caplog.set_level(logging.DEBUG, logger="FreshService")
    assert client.get("/roles") == {"roles": []}
    assert no_sleep == [2.0]
    assert any("ConnectionError on GET /roles, retrying" in r.getMessage() for r in caplog.records)

    api.post(API + "/tickets", body=requests.ConnectionError("connection reset"))
    with pytest.raises(requests.ConnectionError):
        client.post("/tickets", {"subject": "x"}, key="ticket")
    assert sum(1 for c in api.calls if c.request.method == "POST") == 1  # a POST may have landed: no retry


def test_enable_debug_for_integrations(
    settings: FreshServiceSettings, api: responses.RequestsMock, tmp_path: Path
) -> None:
    root = logging.getLogger()
    saved, root.handlers = root.handlers, []  # an application that hasn't set up logging
    try:
        stream = io.StringIO()
        enable_debug(stream=stream, file=tmp_path / "fs.log")
        enable_debug(stream=stream, file=tmp_path / "fs.log")  # idempotent: no doubled lines
        pages(api, "/roles", "roles", [{"id": 1}])
        list(FreshServiceClient(settings).paginate("/roles", "roles"))
        shown = stream.getvalue()
        assert shown.count("GET /roles?page=1&per_page=2 -> 200") == 1
        assert "FreshService.http" in shown and "/roles page 1: 1 records" in shown
        assert "GET /roles?page=1&per_page=2 -> 200" in (tmp_path / "fs.log").read_text()
    finally:
        root.handlers = saved


def test_debug_from_settings(
    settings: FreshServiceSettings, api: responses.RequestsMock, tmp_path: Path
) -> None:
    target = tmp_path / "debug.log"
    client = FreshServiceClient(settings.model_copy(update={"debug_file": target}))
    pages(api, "/roles", "roles", [])
    list(client.paginate("/roles", "roles"))
    assert "GET /roles?page=1&per_page=2 -> 200" in target.read_text()


def test_cli_debug_flags(client: FreshServiceClient, api: responses.RequestsMock, tmp_path: Path) -> None:
    api.get(
        API + "/tickets/9",
        status=404,
        headers={"x-request-id": "r-9"},
        json={"code": "not_found", "message": "No ticket"},
    )
    loud = CliRunner().invoke(main, ["--debug", "tickets", "get", "9"], obj=State(_client=client))
    assert loud.exit_code == 1
    assert "GET /tickets/9 -> 404" in loud.stderr and "Traceback" in loud.stderr
    assert "[request id r-9]" in loud.stderr

    disable_debug()
    target = tmp_path / "cli-debug.log"
    quiet = CliRunner().invoke(
        main, ["--debug-file", str(target), "tickets", "get", "9"], obj=State(_client=client)
    )
    assert quiet.exit_code == 1
    assert "-> 404" not in quiet.stderr and "Error: GET /api/v2/tickets/9 returned HTTP 404" in quiet.stderr
    assert "GET /tickets/9 -> 404" in target.read_text()
