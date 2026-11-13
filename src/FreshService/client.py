"""HTTP client for the Freshservice API v2 (https://api.freshservice.com/v2/).

Authentication is HTTP basic with the API key as the user name and any password ("X").

PAGINATION. List endpoints take `page` (from 1) and `per_page` (at most 100). The
`link` response header carries `rel="next"` while there are more pages and is absent on
the last one, so that header, not the page length, is what ends a listing. An endpoint
that sends no link header at all falls back to "a full page means there may be more".

FILTER AND SEARCH ENDPOINTS (`/tickets/filter?query=`, `/assets?filter=`, ...) return a
fixed 30 records per page, and some stop serving pages at a hard cap (assets: 40 pages,
i.e. 1200 records). Hitting a cap raises FreshServiceTruncatedError instead of returning a
silent fraction of the result.

RATE LIMITS are per account and per minute, shared with every other integration. A 429
carries Retry-After, which is honoured. The client also pauses briefly when
X-RateLimit-Remaining runs low.

ATTACHMENTS go as multipart/form-data: every other field is sent as a form field too,
nested values flattened Rails-style (`tags[]`, `custom_fields[name]`).
"""

from __future__ import annotations

import contextlib
import json
import logging
import mimetypes
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path
from types import TracebackType
from typing import Any, Self, TypeVar

import requests
from pydantic_core import to_jsonable_python

from FreshService.config import FreshServiceSettings

log = logging.getLogger("FreshService")

RETRY_STATUSES = (429, 502, 503, 504)
"""Retried on every method. A POST is only retried on 429, which the server rejects
before doing anything; a 5xx on a POST may have created the record already."""
MAX_PAGES = 10_000
"""Backstop against a paging loop the link-header logic doesn't catch."""
QUERY_PAGE_SIZE = 30
"""Filter/search endpoints always return 30 per page."""
LOW_RATE_LIMIT = 3
"""Pause when this few calls are left in the current minute."""
PACE_SECONDS = 5.0
MAX_ATTACHMENT_BYTES = 40 * 1024 * 1024
"""Freshservice caps the attachments of one ticket/article at 40 MB in total."""

T = TypeVar("T")
R = TypeVar("R")

FileField = tuple[str, tuple[str, bytes, str]]
ProgressHook = Callable[[str, int, int | None], None]
"""progress(label, done, total): total is None when the size isn't known (listings)."""
Attachments = Iterable[str | Path]


class FreshServiceError(RuntimeError):
    """An API call failed. `status` is the HTTP status, `errors` the API's error list."""

    def __init__(
        self, message: str, *, status: int | None = None, errors: list[dict[str, Any]] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.errors = errors or []


class FreshServiceConfigError(FreshServiceError):
    pass


class FreshServiceAuthError(FreshServiceError):
    pass


class FreshServiceNotFoundError(FreshServiceError):
    pass


class FreshServiceTruncatedError(FreshServiceError):
    """A filter/search query matched more records than the endpoint will page through."""


def _form_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value)


def form_fields(data: Mapping[str, Any]) -> list[tuple[str, str]]:
    """A JSON-style body as multipart form fields: lists as `name[]`, dicts as `name[key]`."""
    fields: list[tuple[str, str]] = []

    def add(name: str, value: Any) -> None:
        if value is None:
            return
        if isinstance(value, Mapping):
            for key, item in value.items():
                add(f"{name}[{key}]", item)
        elif isinstance(value, list | tuple | set | frozenset):
            for item in value:
                add(f"{name}[]", item)
        else:
            fields.append((name, _form_scalar(value)))

    for key, value in data.items():
        add(str(key), value)
    return fields


def load_attachments(paths: Attachments, field: str = "attachments[]") -> list[FileField]:
    """Read local files for a multipart upload. Fails before sending anything if one is
    missing or the total is over Freshservice's 40 MB."""
    files: list[FileField] = []
    total = 0
    for raw in paths:
        path = Path(raw)
        if not path.is_file():
            raise FreshServiceError(f"Attachment {str(path)!r} is not a readable file")
        content = path.read_bytes()
        total += len(content)
        if total > MAX_ATTACHMENT_BYTES:
            raise FreshServiceError(
                f"Attachments add up to {total / 1024 / 1024:.1f} MB; Freshservice accepts at most 40 MB"
            )
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        files.append((field, (path.name, content, mime)))
    return files


def extract(payload: Any, key: str | Sequence[str], path: str) -> Any:
    """payload[key] for the first key present. Fails loudly if none is: a renamed envelope
    must not read as "no records"."""
    keys = (key,) if isinstance(key, str) else tuple(key)
    if isinstance(payload, dict):
        for name in keys:
            if name in payload:
                return payload[name]
    raise FreshServiceError(
        f"/api/v2{path} returned no {' or '.join(repr(k) for k in keys)} in its response. "
        f"The endpoint's shape may have changed - check the API reference."
    )


def _as_list(value: Any, path: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise FreshServiceError(f"/api/v2{path} returned {type(value).__name__}, expected a list")
    return value


def _as_dict(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FreshServiceError(f"/api/v2{path} returned {type(value).__name__}, expected an object")
    return value


def _retry_delay(response: requests.Response, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After", "").strip()
    if retry_after.isdigit():
        return float(retry_after)
    return float(min(2**attempt, 60))


class FreshServiceClient:
    """Authenticated access to one Freshservice account.

        with FreshServiceClient() as client:          # FRESH_* from env / .env
            ticket = get_ticket(client, 1234)

    Thread-safe for the fan-outs this library does (parallel GETs through one session).
    Pass `audit_log` to append every POST/PUT/DELETE to a JSON-lines file, and `progress`
    (label, done, total) to hear how a long listing or fan-out is getting on.
    """

    def __init__(
        self,
        settings: FreshServiceSettings | None = None,
        session: requests.Session | None = None,
        *,
        audit_log: Path | None = None,
        progress: ProgressHook | None = None,
    ) -> None:
        self.settings = settings or FreshServiceSettings()
        missing = self.settings.missing()
        if missing:
            raise FreshServiceConfigError(f"{' and '.join(missing)} not set (environment or .env)")
        self.session = session or requests.Session()
        self.session.auth = (self.settings.api_key.get_secret_value(), "X")
        self.audit_log = audit_log
        self.progress = progress
        """Called as progress(label, done, total) while listing and fanning out. None: silent."""
        self._local = threading.local()
        self._lock = threading.Lock()
        self._memo: dict[str, Any] = {}

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self.session.close()

    def memo(self, key: str, factory: Callable[[], T]) -> T:
        """Per-client cache for lookups that don't change during a run (asset types, fields)."""
        with self._lock:
            if key in self._memo:
                return self._memo[key]  # type: ignore[no-any-return]
        value = factory()
        with self._lock:
            return self._memo.setdefault(key, value)  # type: ignore[no-any-return]

    def parallel(self, func: Callable[[T], R], items: Iterable[T], *, label: str | None = None) -> list[R]:
        """func over items with up to FRESH_MAX_WORKERS requests in flight; results in order.

        With a label, progress is reported as done/total. Listings inside func don't report
        their own progress (they would interleave).
        """
        work = list(items)

        def quietly(item: T) -> R:
            was = getattr(self._local, "quiet", False)
            self._local.quiet = True
            try:
                return func(item)
            finally:
                self._local.quiet = was

        if len(work) <= 1 or self.settings.max_workers <= 1:
            results = []
            for done, item in enumerate(work, start=1):
                results.append(quietly(item))
                if label:
                    self.report(label, done, len(work))
            return results
        with ThreadPoolExecutor(max_workers=self.settings.max_workers) as pool:
            futures = [pool.submit(quietly, item) for item in work]
            if label:
                for done, _ in enumerate(as_completed(futures), start=1):
                    self.report(label, done, len(work))
            return [future.result() for future in futures]

    def report(self, label: str, done: int, total: int | None = None) -> None:
        """Progress for whoever set `progress` (the CLI does; the library alone is silent).
        total None means "records so far" (most listings don't say how many there are)."""
        if self.progress is not None and not getattr(self._local, "quiet", False):
            self.progress(label, done, total)

    # -- requests ---------------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        data: list[tuple[str, str]] | None = None,
        files: list[FileField] | None = None,
    ) -> requests.Response:
        """One call to /api/v2{path}. Retries 429 (Retry-After) and 502-504; raises on >= 400."""
        url = self.settings.api_root + path
        retryable = RETRY_STATUSES if method in ("GET", "PUT", "DELETE") else (429,)
        response: requests.Response | None = None

        for attempt in range(1, self.settings.max_attempts + 1):
            response = self.session.request(
                method,
                url,
                params=dict(params) if params else None,
                json=json_body,
                data=data,
                files=files,
                headers={"Accept": "application/json"},
                timeout=self.settings.timeout,
            )
            self._pace(response)
            if response.status_code in retryable and attempt < self.settings.max_attempts:
                delay = _retry_delay(response, attempt)
                log.info("HTTP %s on %s %s, retrying in %.0fs", response.status_code, method, path, delay)
                time.sleep(delay)
                continue
            break

        assert response is not None  # max_attempts >= 1, so the loop always assigns
        if method != "GET":
            self._audit(method, path, params, json_body if json_body is not None else data, files, response)
        if response.status_code >= 400:
            raise self._error(method, path, response)
        return response

    def _pace(self, response: requests.Response) -> None:
        remaining = response.headers.get("X-RateLimit-Remaining", "").strip()
        if not remaining.isdigit():
            return
        log.debug("rate limit: %s of %s left", remaining, response.headers.get("X-RateLimit-Total", "?"))
        if int(remaining) <= LOW_RATE_LIMIT and response.status_code != 429:
            log.info("only %s API calls left this minute; pausing %.0fs", remaining, PACE_SECONDS)
            time.sleep(PACE_SECONDS)

    def _error(self, method: str, path: str, response: requests.Response) -> FreshServiceError:
        body: Any = None
        with contextlib.suppress(ValueError):
            body = response.json()
        message = ""
        errors: list[dict[str, Any]] = []
        if isinstance(body, dict):
            message = str(body.get("description") or body.get("message") or "")
            raw = body.get("errors")
            if isinstance(raw, list):
                errors = [error for error in raw if isinstance(error, dict)]
            elif body.get("code"):
                errors = [body]
        details = "; ".join(
            " ".join(
                part
                for part in (
                    f"{error['field']}:" if error.get("field") else "",
                    "" if error.get("message") == message else str(error.get("message") or ""),
                    f"({error['code']})" if error.get("code") else "",
                )
                if part
            )
            for error in errors
        )
        status = response.status_code
        text = f"{method} /api/v2{path} returned HTTP {status}"
        if message:
            text += f": {message}"
        if details and details != message:
            text += f" - {details}"
        if status == 401:
            return FreshServiceAuthError(text + ". Check FRESH_API_KEY and FRESH_DOMAIN.", status=status)
        if status == 403:
            return FreshServiceAuthError(
                text + ". The API key's agent lacks the permission, or the plan lacks the feature.",
                status=status,
                errors=errors,
            )
        if status == 404:
            return FreshServiceNotFoundError(text, status=status, errors=errors)
        return FreshServiceError(text, status=status, errors=errors)

    def _audit(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None,
        body: Any,
        files: list[FileField] | None,
        response: requests.Response,
    ) -> None:
        if self.audit_log is None:
            return
        entry = {
            "time": datetime.now(UTC).isoformat(timespec="seconds"),
            "method": method,
            "path": path,
            "params": dict(params) if params else None,
            "body": body,
            "attachments": [file[1][0] for file in files] if files else None,
            "status": response.status_code,
        }
        line = json.dumps(entry, default=str, ensure_ascii=False)
        with self._lock, self.audit_log.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    # -- JSON helpers ----------------------------------------------------------

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        response = self.request("GET", path, params=params)
        return response.json() if response.content else None

    def get_object(
        self, path: str, key: str | Sequence[str], params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """GET a single record, unwrapped from its envelope ({"ticket": {...}})."""
        return _as_dict(extract(self.get(path, params), key, path), path)

    def get_list(
        self, path: str, key: str | Sequence[str], params: Mapping[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """GET an unpaged list, unwrapped from its envelope."""
        return _as_list(extract(self.get(path, params), key, path), path)

    def send(
        self,
        method: str,
        path: str,
        key: str | Sequence[str] | None,
        body: Mapping[str, Any] | None = None,
        *,
        attachments: Attachments = (),
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        """POST/PUT a body (JSON, or multipart when there are attachments); returns payload[key]."""
        files = load_attachments(attachments)
        if files:
            response = self.request(method, path, params=params, data=form_fields(body or {}), files=files)
        else:
            json_body = to_jsonable_python(dict(body)) if body is not None else None
            response = self.request(method, path, params=params, json_body=json_body)
        if not response.content:
            return None
        payload = response.json()
        return extract(payload, key, path) if key else payload

    def post(
        self,
        path: str,
        body: Mapping[str, Any] | None = None,
        *,
        key: str | Sequence[str] | None = None,
        attachments: Attachments = (),
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        return self.send("POST", path, key, body, attachments=attachments, params=params)

    def put(
        self,
        path: str,
        body: Mapping[str, Any] | None = None,
        *,
        key: str | Sequence[str] | None = None,
        attachments: Attachments = (),
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        return self.send("PUT", path, key, body, attachments=attachments, params=params)

    def delete(self, path: str, params: Mapping[str, Any] | None = None) -> None:
        self.request("DELETE", path, params=params)

    # -- pagination ------------------------------------------------------------

    def paginate(
        self,
        path: str,
        key: str | Sequence[str],
        params: Mapping[str, Any] | None = None,
        *,
        page_size: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Every record of a paged list endpoint, in order.

        Pages are independent (`page=N`), so after the first they are fetched several at a
        time: 1, 1, 2, 4, ... up to FRESH_MAX_WORKERS per round. A listing ends at the first page
        without `link: rel="next"` (or an empty one); pages fetched past the end are discarded,
        errors on them included. At most max_workers - 1 extra calls per listing.
        """
        size = page_size or self.settings.page_size
        base = dict(params or {})
        label = path.strip("/")
        previous: tuple[str, str] | None = None
        count = 0
        page, batch = 1, 1
        while page <= MAX_PAGES:
            numbers = list(range(page, min(page + batch, MAX_PAGES + 1)))
            for number, outcome in zip(
                numbers, self._fetch_pages(path, key, base, numbers, size), strict=True
            ):
                if isinstance(outcome, BaseException):
                    raise outcome
                records, has_next = outcome
                if not records:
                    return
                marker = (json.dumps(records[0], sort_keys=True), json.dumps(records[-1], sort_keys=True))
                if marker == previous:
                    raise FreshServiceError(
                        f"/api/v2{path} served page {number} identical to page {number - 1}"
                    )
                previous = marker
                count += len(records)
                self.report(label, count)
                yield from records
                if not has_next:
                    return
            page += len(numbers)
            batch = min(batch * 2 if page > 2 else 1, self.settings.max_workers)
        raise FreshServiceError(f"/api/v2{path} exceeded {MAX_PAGES} pages; refusing to continue.")

    def _fetch_pages(
        self, path: str, key: str | Sequence[str], params: dict[str, Any], numbers: list[int], size: int
    ) -> list[tuple[list[dict[str, Any]], bool] | Exception]:
        """These pages, in order, each as (records, has_next) or the exception fetching it raised
        (raised by paginate only if the page turns out to be needed)."""

        def fetch(number: int) -> tuple[list[dict[str, Any]], bool] | Exception:
            try:
                response = self.request("GET", path, params={**params, "page": number, "per_page": size})
                records = _as_list(extract(response.json(), key, path), path) if response.content else []
                return records, self._has_next(response, len(records), size)
            except Exception as exc:  # handed back; paginate raises it only if the page is needed
                return exc

        if len(numbers) == 1:
            return [fetch(numbers[0])]
        with ThreadPoolExecutor(max_workers=len(numbers)) as pool:
            return list(pool.map(fetch, numbers))

    def paginate_query(
        self,
        path: str,
        key: str | Sequence[str],
        params: Mapping[str, Any],
        *,
        max_pages: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Every record of a filter/search endpoint (30 per page, `page` only).

        With `max_pages`, a result that is still going on the last page the endpoint
        serves raises FreshServiceTruncatedError: narrow the query rather than trust a fraction.
        """
        limit = max_pages or MAX_PAGES
        for page in range(1, limit + 1):
            response = self.request("GET", path, params={**params, "page": page})
            records = _as_list(extract(response.json(), key, path), path) if response.content else []
            if not records:
                return
            yield from records
            if not self._has_next(response, len(records), QUERY_PAGE_SIZE):
                return
        raise FreshServiceTruncatedError(
            f"/api/v2{path} matched more than {limit * QUERY_PAGE_SIZE} records, the most the endpoint "
            f"pages through. Narrow the query (e.g. by type, date or workspace)."
        )

    @staticmethod
    def _has_next(response: requests.Response, count: int, size: int) -> bool:
        if "next" in response.links:
            return True
        if response.headers.get("link"):
            return False  # a link header without rel="next": the last page
        return count >= size  # no link header at all: a full page may have a successor
