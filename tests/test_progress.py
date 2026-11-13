from __future__ import annotations

import io

import pytest
import responses
from click.testing import CliRunner

from FreshService import FreshServiceClient, FreshServiceError, FreshServiceSettings
from FreshService.cli import State, StderrProgress, main
from tests.conftest import API, get, pages


@pytest.fixture
def wide(settings: FreshServiceSettings, api: responses.RequestsMock) -> FreshServiceClient:
    """A client allowed 4 requests at once, so page rounds go 1, 1, 2, 4."""
    return FreshServiceClient(settings.model_copy(update={"max_workers": 4}))


def test_pages_come_back_in_order_across_parallel_rounds(
    wide: FreshServiceClient, api: responses.RequestsMock
) -> None:
    rows = [{"id": i} for i in range(1, 16)]  # 8 pages of 2 (the last short)
    pages(api, "/vendors", "vendors", rows)
    assert [row["id"] for row in wide.paginate("/vendors", "vendors")] == list(range(1, 16))
    asked = sorted(int((c.request.url or "").split("page=")[1].split("&")[0]) for c in api.calls)
    # rounds [1], [2], [3, 4], [5..8]: page 8 is the last, so nothing past it is asked for
    assert asked == [1, 2, 3, 4, 5, 6, 7, 8]


def test_errors_past_the_end_are_ignored_but_not_on_needed_pages(
    wide: FreshServiceClient, api: responses.RequestsMock
) -> None:
    rows = [{"id": i} for i in range(1, 6)]  # pages 1-3; page 4 is never registered
    pages(api, "/roles", "roles", rows)
    assert len(list(wide.paginate("/roles", "roles"))) == 5  # page 4 failed (unregistered), unneeded

    get(
        api,
        "/groups",
        {"groups": [{"id": 1}, {"id": 2}]},
        {"page": 1, "per_page": 2},
        headers={"link": '<x>; rel="next"'},
    )
    api.get(API + "/groups", status=400, json={"description": "bad page"})
    with pytest.raises(FreshServiceError, match="bad page"):
        list(wide.paginate("/groups", "groups"))


def test_progress_is_silent_unless_asked_and_quiet_inside_fan_outs(
    client: FreshServiceClient, api: responses.RequestsMock
) -> None:
    pages(api, "/vendors", "vendors", [{"id": i} for i in range(1, 6)])
    list(client.paginate("/vendors", "vendors"))  # no hook: nothing to report to

    seen: list[tuple[str, int, int | None]] = []
    client.progress = lambda label, done, total: seen.append((label, done, total))
    list(client.paginate("/vendors", "vendors"))
    assert seen == [("vendors", 2, None), ("vendors", 4, None), ("vendors", 5, None)]

    seen.clear()
    results = client.parallel(
        lambda n: len(list(client.paginate("/vendors", "vendors"))) + n, [0, 1, 2], label="work"
    )
    assert results == [5, 6, 7]
    assert sorted(seen) == [("work", 1, 3), ("work", 2, 3), ("work", 3, 3)]  # no nested "vendors" lines


def test_stderr_progress_line() -> None:
    stream = io.StringIO()
    progress = StderrProgress(stream)
    progress("applications", 3400, None)
    progress("applications", 3500, None)  # same label within 0.1s: skipped
    progress("installations", 1, 120)  # a new label always shows
    progress("installations", 120, 120)  # so does a final count
    assert stream.getvalue() == (
        "\r\x1b[Kapplications: 3,400 records..."
        "\r\x1b[Kinstallations: 1/120..."
        "\r\x1b[Kinstallations: 120/120..."
    )
    progress.clear()
    assert stream.getvalue().endswith("\r\x1b[K")
    progress.clear()  # nothing shown: nothing written
    assert stream.getvalue().count("\r\x1b[K") == 4


def test_cli_progress_on_request_only(client: FreshServiceClient, api: responses.RequestsMock) -> None:
    pages(api, "/vendors", "vendors", [{"id": 1, "name": "Mozilla"}])
    pages(api, "/applications", "applications", [{"id": 1, "name": "Firefox", "publisher_id": 1}])
    quiet = CliRunner().invoke(main, ["publishers", "list"], obj=State(_client=client))
    assert quiet.exit_code == 0 and "record..." not in quiet.stderr  # not a terminal: off by default
    loud = CliRunner().invoke(main, ["--progress", "publishers", "list"], obj=State(_client=client))
    assert loud.exit_code == 0, loud.output
    assert "vendors: 1 record..." in loud.stderr and "applications: 1 record..." in loud.stderr
    assert loud.stdout.splitlines()[1].split()[:2] == ["1", "Mozilla"]  # the table is unaffected
