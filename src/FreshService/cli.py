"""`freshservice` - the library's functions as JSON on stdout.

    freshservice tickets note 1234 --body-file update.md --attach report.pdf
    freshservice changes create --subject "Patch FW" --body "..." --email me@example.com
    freshservice articles list --mine
    freshservice software where --vendor Mozilla --name Firefox --version "<128" --summary
    freshservice assets tag-unknown --type Firewall --type Switch           # plan only
    freshservice assets tag-unknown --type Firewall --type Switch --apply   # write

Logs and errors go to stderr, so stdout is always valid JSON (or nothing, on error).
Bodies are Markdown (converted to HTML) unless --html is given.

Bulk operations (tag-unknown, set-product-vendor, vendors merge, software merge,
software delete) print the plan of what they would change; --apply makes the changes
and prints the plan with each action's outcome. Single deletes ask for confirmation
unless --yes is given.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, TypeVar

import click
import requests
from pydantic_core import to_jsonable_python

from FreshService import alerts as alerts_api
from FreshService import announcements as announcements_api
from FreshService import assets as assets_api
from FreshService import catalog as catalog_api
from FreshService import changes as changes_api
from FreshService import maintenance
from FreshService import org as org_api
from FreshService import service_requests as requests_api
from FreshService import software as software_api
from FreshService import solutions as solutions_api
from FreshService import tickets as tickets_api
from FreshService.client import FreshServiceClient, FreshServiceError
from FreshService.config import FreshServiceSettings
from FreshService.formatting import markdown_to_html
from FreshService.models import (
    ArticleStatus,
    ArticleType,
    ChangeStatus,
    ChangeType,
    Level,
    MaintenancePlan,
    Priority,
    Risk,
    TicketStatus,
    code,
)

log = logging.getLogger("FreshService")

F = TypeVar("F", bound=Callable[..., Any])


@dataclass
class State:
    env_file: Path | None = None
    output: Path | None = None
    compact: bool = False
    audit_log: Path | None = None
    _client: FreshServiceClient | None = field(default=None, repr=False)

    @property
    def client(self) -> FreshServiceClient:
        if self._client is None:
            settings = (
                FreshServiceSettings(_env_file=self.env_file) if self.env_file else FreshServiceSettings()
            )
            self._client = FreshServiceClient(settings, audit_log=self.audit_log)
        return self._client


pass_state = click.make_pass_decorator(State)


def emit(state: State, data: Any) -> None:
    """Models, lists of models and dicts of them -> JSON on stdout (or --output)."""
    text = json.dumps(
        to_jsonable_python(data, by_alias=False),
        indent=None if state.compact else 2,
        ensure_ascii=False,
    )
    if state.output:
        state.output.parent.mkdir(parents=True, exist_ok=True)
        state.output.write_text(text + "\n", encoding="utf-8")
        count = f"{len(data)} records" if isinstance(data, list | dict) else "1 record"
        click.echo(f"Wrote {count} to {state.output}", err=True)
    else:
        click.echo(text)


# -- option helpers ---------------------------------------------------------------------


def _parse_value(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text


def parse_fields(values: Sequence[str]) -> dict[str, Any]:
    """--field name=value (repeatable). Values are JSON if they parse (numbers, true, [..]),
    else strings. Dotted names nest: custom_fields.cf_team=Ops."""
    fields: dict[str, Any] = {}
    for item in values:
        name, sep, value = item.partition("=")
        if not sep or not name.strip():
            raise click.BadParameter(f"{item!r} is not name=value", param_hint="--field")
        target = fields
        *parents, leaf = name.strip().split(".")
        for parent in parents:
            target = target.setdefault(parent, {})
            if not isinstance(target, dict):
                raise click.BadParameter(f"{name!r} nests under a non-object", param_hint="--field")
        target[leaf] = _parse_value(value)
    return fields


def _enum_callback(enum: Any) -> Callable[[click.Context, click.Parameter, Any], Any]:
    def convert(ctx: click.Context, param: click.Parameter, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, tuple):
            return tuple(convert(ctx, param, item) for item in value)
        try:
            return code(enum, value)
        except ValueError as exc:
            raise click.BadParameter(str(exc)) from None

    return convert


def enum_option(*names: str, enum: Any, help: str, multiple: bool = False) -> Callable[[F], F]:
    choices = "|".join(member.name.lower().replace("_", "-") for member in enum)
    return click.option(
        *names,
        callback=_enum_callback(enum),
        metavar=f"[{choices}]",
        multiple=multiple,
        help=help,
    )


def _parse_when(ctx: click.Context, param: click.Parameter, value: str | None) -> datetime | date | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value) if "T" in value or " " in value else date.fromisoformat(value)
    except ValueError:
        raise click.BadParameter(f"{value!r} is not YYYY-MM-DD or an ISO datetime") from None


def when_option(*names: str, help: str) -> Callable[[F], F]:
    return click.option(*names, callback=_parse_when, metavar="DATE", help=help)


attach_option = click.option(
    "--attach",
    "attachments",
    multiple=True,
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    help="Attach a file (repeatable; 40 MB in total).",
)
field_option = click.option(
    "--field", "fields", multiple=True, metavar="NAME=VALUE", help="Any other API field (repeatable)."
)
yes_option = click.option("--yes", is_flag=True, help="Don't ask for confirmation.")
apply_option = click.option("--apply", is_flag=True, help="Make the changes (default: print the plan only).")


def body_options[G: Callable[..., Any]](f: G) -> G:
    f = click.option("--html", is_flag=True, help="The body is HTML already; send it as is.")(f)
    f = click.option(
        "--body-file",
        type=click.Path(path_type=Path, allow_dash=True, dir_okay=False),
        help="Read the body from a Markdown file ('-' for stdin).",
    )(f)
    f = click.option("--body", help="The body, as Markdown.")(f)
    return f


def read_body(body: str | None, body_file: Path | None, html: bool, *, required: bool = True) -> str | None:
    if body is not None and body_file is not None:
        raise click.UsageError("Give --body or --body-file, not both")
    text = body
    if body_file is not None:
        text = sys.stdin.read() if str(body_file) == "-" else body_file.read_text(encoding="utf-8")
    if text is None or not text.strip():
        if required:
            raise click.UsageError("A body is required: --body or --body-file")
        return None
    return text if html else markdown_to_html(text)


def confirm(yes: bool, what: str) -> None:
    if not yes:
        click.confirm(f"Delete {what}?", abort=True, err=True)


def run_plan(state: State, plan: MaintenancePlan, apply: bool) -> None:
    for warning in plan.warnings:
        log.warning("%s", warning)
    if apply:
        plan = maintenance.apply_plan(state.client, plan)
        failed = plan.summary.get("failed", 0)
        if failed:
            log.error("%s of %s actions failed; see their 'error'", failed, len(plan.actions))
    else:
        log.warning("Plan only: %d change(s). Run again with --apply to make them.", len(plan.actions))
    emit(state, plan)


class Group(click.Group):
    """Turns API and input errors into a one-line message and exit code 1."""

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except (FreshServiceError, ValueError, requests.RequestException) as exc:
            raise click.ClickException(str(exc)) from exc


@click.group(cls=Group)
@click.option(
    "--env-file",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    help="Read FRESH_* settings from this file instead of ./.env.",
)
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path, dir_okay=False),
    help="Write JSON here instead of stdout.",
)
@click.option("--compact", is_flag=True, help="One-line JSON.")
@click.option(
    "--audit-log",
    type=click.Path(path_type=Path, dir_okay=False),
    help="Append every POST/PUT/DELETE to this JSON-lines file.",
)
@click.option("-v", "--verbose", count=True, help="-v for progress, -vv for HTTP details.")
@click.version_option(package_name="freshservice-library")
@click.pass_context
def main(
    ctx: click.Context,
    env_file: Path | None,
    output: Path | None,
    compact: bool,
    audit_log: Path | None,
    verbose: int,
) -> None:
    """Freshservice API: tickets, requests, changes, articles, announcements, alerts, assets, software."""
    logging.basicConfig(
        stream=sys.stderr,
        level=max(logging.DEBUG, logging.WARNING - 10 * verbose),
        format="%(levelname)s %(message)s",
    )
    if not isinstance(ctx.obj, State):
        ctx.obj = State()
    ctx.obj.env_file, ctx.obj.output, ctx.obj.compact, ctx.obj.audit_log = (
        env_file,
        output,
        compact,
        audit_log,
    )


# -- tickets ------------------------------------------------------------------------------


@main.group()
def tickets() -> None:
    """Tickets (incidents and service requests), notes and replies."""


@tickets.command("get")
@click.argument("ticket_id", type=int)
@click.option("--include", help="Comma-separated embeds: conversations,requester,stats,assets,...")
@pass_state
def tickets_get(state: State, ticket_id: int, include: str | None) -> None:
    """One ticket."""
    embeds = [part.strip() for part in (include or "").split(",") if part.strip()]
    emit(state, tickets_api.get_ticket(state.client, ticket_id, embeds))


@tickets.command("list")
@when_option("--since", help="Updated since (default: the API's last 30 days of created tickets).")
@click.option(
    "--predefined",
    type=click.Choice(["new_and_my_open", "watching", "spam", "deleted"]),
    help="A built-in view.",
)
@click.option("--email", help="Requester email.")
@click.option("--requester-id", type=int)
@click.option("--type", "ticket_type", help='e.g. "Service Request" or "Incident".')
@pass_state
def tickets_list(
    state: State,
    since: date | None,
    predefined: str | None,
    email: str | None,
    requester_id: int | None,
    ticket_type: str | None,
) -> None:
    """List tickets."""
    emit(
        state,
        tickets_api.list_tickets(
            state.client,
            predefined=predefined,
            email=email,
            requester_id=requester_id,
            updated_since=since,
            type=ticket_type,
        ),
    )


@tickets.command("filter")
@click.argument("query")
@pass_state
def tickets_filter(state: State, query: str) -> None:
    """Tickets matching a filter query, e.g. "status:2 AND priority:>3"."""
    emit(state, tickets_api.filter_tickets(state.client, query))


@tickets.command("fields")
@pass_state
def tickets_fields(state: State) -> None:
    """Ticket form fields (names for filter queries and --field)."""
    emit(state, tickets_api.list_ticket_fields(state.client))


def _templates(state: State, path: Path | None) -> dict[str, dict[str, Any]]:
    source = path or state.client.settings.template_file
    if source is None:
        raise click.UsageError("No templates: pass --templates or set FRESH_TEMPLATE_FILE")
    return tickets_api.load_templates(source)


@tickets.command("templates")
@click.option("--templates", type=click.Path(path_type=Path, exists=True, dir_okay=False))
@pass_state
def tickets_templates(state: State, templates: Path | None) -> None:
    """The ticket templates in FRESH_TEMPLATE_FILE (or --templates)."""
    emit(state, _templates(state, templates))


def _ticket_overrides(
    priority: int | None,
    status: int | None,
    urgency: int | None,
    impact: int | None,
    group_id: int | None,
    responder_id: int | None,
    category: str | None,
    tags: Sequence[str],
    fields: Sequence[str],
) -> dict[str, Any]:
    values: dict[str, Any] = {
        "priority": priority,
        "status": status,
        "urgency": urgency,
        "impact": impact,
        "group_id": group_id,
        "responder_id": responder_id,
        "category": category,
        "tags": list(tags) or None,
        **parse_fields(fields),
    }
    return {key: value for key, value in values.items() if value is not None}


def ticket_field_options[G: Callable[..., Any]](f: G) -> G:
    for decorator in reversed(
        [
            enum_option("--priority", enum=Priority, help="Priority."),
            enum_option("--status", enum=TicketStatus, help="Status."),
            enum_option("--urgency", enum=Level, help="Urgency."),
            enum_option("--impact", enum=Level, help="Impact."),
            click.option("--group-id", type=int, help="Agent group."),
            click.option("--responder-id", type=int, help="Assigned agent."),
            click.option("--category"),
            click.option("--tag", "tags", multiple=True, help="Tag (repeatable)."),
            field_option,
            attach_option,
        ]
    ):
        f = decorator(f)
    return f


@tickets.command("create")
@click.option("--subject")
@body_options
@click.option("--template", help="Start from this template (FRESH_TEMPLATE_FILE / --templates).")
@click.option("--templates", type=click.Path(path_type=Path, exists=True, dir_okay=False))
@click.option("--email", help="Requester email (default: the template's / FRESH_DEFAULT_EMAIL).")
@click.option("--requester-id", type=int)
@ticket_field_options
@pass_state
def tickets_create(
    state: State,
    subject: str | None,
    body: str | None,
    body_file: Path | None,
    html: bool,
    template: str | None,
    templates: Path | None,
    email: str | None,
    requester_id: int | None,
    priority: int | None,
    status: int | None,
    urgency: int | None,
    impact: int | None,
    group_id: int | None,
    responder_id: int | None,
    category: str | None,
    tags: tuple[str, ...],
    fields: tuple[str, ...],
    attachments: tuple[Path, ...],
) -> None:
    """Create a ticket."""
    description = read_body(body, body_file, html)
    assert description is not None
    values: dict[str, Any] = {}
    if template:
        values = tickets_api.ticket_from_template(
            state.client.settings,
            _templates(state, templates),
            template,
            description=description,
            subject=subject,
        )
    else:
        if not subject:
            raise click.UsageError("--subject is required without --template")
        values = {
            "subject": subject,
            "description": description,
            "email": state.client.settings.default_email,
        }
    values.update(
        _ticket_overrides(priority, status, urgency, impact, group_id, responder_id, category, tags, fields)
    )
    if email:
        values["email"] = email
    if requester_id is not None:
        values["requester_id"] = requester_id
        if not email:
            values.pop("email", None)
    emit(state, tickets_api.create_ticket(state.client, attachments=attachments, **values))


@tickets.command("update")
@click.argument("ticket_id", type=int)
@click.option("--subject")
@ticket_field_options
@pass_state
def tickets_update(
    state: State,
    ticket_id: int,
    subject: str | None,
    priority: int | None,
    status: int | None,
    urgency: int | None,
    impact: int | None,
    group_id: int | None,
    responder_id: int | None,
    category: str | None,
    tags: tuple[str, ...],
    fields: tuple[str, ...],
    attachments: tuple[Path, ...],
) -> None:
    """Change ticket fields and/or add attachments (--tag replaces the tags)."""
    values = _ticket_overrides(
        priority, status, urgency, impact, group_id, responder_id, category, tags, fields
    )
    if subject:
        values["subject"] = subject
    if not values and not attachments:
        raise click.UsageError("Nothing to change")
    emit(state, tickets_api.update_ticket(state.client, ticket_id, attachments=attachments, **values))


def _note_command(group: click.Group, name: str, noun: str) -> None:
    @group.command(name)
    @click.argument("ticket_id", type=int)
    @body_options
    @click.option("--public", is_flag=True, help="Visible to the requester (default: private).")
    @click.option("--notify", "notify_emails", multiple=True, help="Email to notify (repeatable).")
    @attach_option
    @pass_state
    def note(
        state: State,
        ticket_id: int,
        body: str | None,
        body_file: Path | None,
        html: bool,
        public: bool,
        notify_emails: tuple[str, ...],
        attachments: tuple[Path, ...],
    ) -> None:
        text = read_body(body, body_file, html)
        assert text is not None
        emit(
            state,
            tickets_api.add_note(
                state.client,
                ticket_id,
                text,
                private=not public,
                notify_emails=notify_emails,
                attachments=attachments,
            ),
        )

    note.__doc__ = f"Add a note (private by default) to a {noun}, with attachments."


_note_command(tickets, "note", "ticket")


@tickets.command("reply")
@click.argument("ticket_id", type=int)
@body_options
@click.option("--cc", "cc_emails", multiple=True)
@click.option("--bcc", "bcc_emails", multiple=True)
@attach_option
@pass_state
def tickets_reply(
    state: State,
    ticket_id: int,
    body: str | None,
    body_file: Path | None,
    html: bool,
    cc_emails: tuple[str, ...],
    bcc_emails: tuple[str, ...],
    attachments: tuple[Path, ...],
) -> None:
    """Reply to the requester (sends an email)."""
    text = read_body(body, body_file, html)
    assert text is not None
    emit(
        state,
        tickets_api.reply(
            state.client, ticket_id, text, cc_emails=cc_emails, bcc_emails=bcc_emails, attachments=attachments
        ),
    )


@tickets.command("conversations")
@click.argument("ticket_id", type=int)
@pass_state
def tickets_conversations(state: State, ticket_id: int) -> None:
    """All notes and replies on a ticket."""
    emit(state, tickets_api.list_conversations(state.client, ticket_id))


@tickets.command("delete")
@click.argument("ticket_id", type=int)
@yes_option
@pass_state
def tickets_delete(state: State, ticket_id: int, yes: bool) -> None:
    """Move a ticket to trash."""
    confirm(yes, f"ticket {ticket_id}")
    tickets_api.delete_ticket(state.client, ticket_id)
    emit(state, {"deleted": ticket_id})


# -- service requests -------------------------------------------------------------------


@main.group("requests")
def service_requests() -> None:
    """Service requests and the service catalog."""


@service_requests.command("categories")
@pass_state
def requests_categories(state: State) -> None:
    """Service catalog categories."""
    emit(state, requests_api.list_catalog_categories(state.client))


@service_requests.command("items")
@click.option("--category-id", type=int)
@click.option("--search", help="Search term instead of listing.")
@pass_state
def requests_items(state: State, category_id: int | None, search: str | None) -> None:
    """Service catalog items."""
    if search:
        emit(state, requests_api.search_catalog_items(state.client, search))
    else:
        emit(state, requests_api.list_catalog_items(state.client, category_id=category_id))


@service_requests.command("item")
@click.argument("display_id", type=int)
@pass_state
def requests_item(state: State, display_id: int) -> None:
    """One catalog item, with its form fields."""
    emit(state, requests_api.get_catalog_item(state.client, display_id))


@service_requests.command("create")
@click.argument("item_display_id", type=int)
@click.option("--quantity", type=int, default=1, show_default=True)
@click.option("--email", help="Requester (default: the API key's agent).")
@click.option("--requested-for", help="Email of whom it is for.")
@click.option(
    "--field", "fields", multiple=True, metavar="NAME=VALUE", help="An item form field (repeatable)."
)
@attach_option
@pass_state
def requests_create(
    state: State,
    item_display_id: int,
    quantity: int,
    email: str | None,
    requested_for: str | None,
    fields: tuple[str, ...],
    attachments: tuple[Path, ...],
) -> None:
    """Request a catalog item. Attachments are added as a note on the new request."""
    emit(
        state,
        requests_api.create_service_request(
            state.client,
            item_display_id,
            quantity=quantity,
            email=email,
            requested_for=requested_for,
            custom_fields=parse_fields(fields) or None,
            attachments=attachments,
        ),
    )


@service_requests.command("requested-items")
@click.argument("ticket_id", type=int)
@pass_state
def requests_requested_items(state: State, ticket_id: int) -> None:
    """The items requested on a service request."""
    emit(state, requests_api.list_requested_items(state.client, ticket_id))


_note_command(service_requests, "note", "service request")


# -- changes -------------------------------------------------------------------------------


@main.group()
def changes() -> None:
    """Changes and change notes."""


@changes.command("get")
@click.argument("change_id", type=int)
@pass_state
def changes_get(state: State, change_id: int) -> None:
    """One change."""
    emit(state, changes_api.get_change(state.client, change_id))


@changes.command("list")
@click.option("--query", help='Filter query, e.g. "priority:4 OR risk:4".')
@click.option("--view", help="A change view (name or id).")
@when_option("--since", help="Updated since.")
@click.option("--order-by")
@click.option("--order-type", type=click.Choice(["asc", "desc"]))
@pass_state
def changes_list(
    state: State,
    query: str | None,
    view: str | None,
    since: date | None,
    order_by: str | None,
    order_type: str | None,
) -> None:
    """List changes."""
    emit(
        state,
        changes_api.list_changes(
            state.client,
            query=query,
            view=view,
            updated_since=since,
            order_by=order_by,
            order_type=order_type,
        ),
    )


@changes.command("fields")
@pass_state
def changes_fields(state: State) -> None:
    """Change form fields."""
    emit(state, changes_api.list_change_fields(state.client))


def change_field_options[G: Callable[..., Any]](f: G) -> G:
    for decorator in reversed(
        [
            enum_option("--priority", enum=Priority, help="Priority."),
            enum_option("--impact", enum=Level, help="Impact."),
            enum_option("--risk", enum=Risk, help="Risk."),
            enum_option("--type", "change_type", enum=ChangeType, help="Change type."),
            enum_option("--status", enum=ChangeStatus, help="Status."),
            when_option("--start", help="Planned start."),
            when_option("--end", help="Planned end."),
            click.option("--agent-id", type=int),
            click.option("--group-id", type=int),
            click.option("--department-id", type=int),
            click.option("--category"),
            field_option,
            attach_option,
        ]
    ):
        f = decorator(f)
    return f


def _change_values(
    priority: int | None,
    impact: int | None,
    risk: int | None,
    change_type: int | None,
    status: int | None,
    start: date | None,
    end: date | None,
    agent_id: int | None,
    group_id: int | None,
    department_id: int | None,
    category: str | None,
    fields: Sequence[str],
) -> dict[str, Any]:
    values: dict[str, Any] = {
        "priority": priority,
        "impact": impact,
        "risk": risk,
        "change_type": change_type,
        "status": status,
        "planned_start_date": start,
        "planned_end_date": end,
        "agent_id": agent_id,
        "group_id": group_id,
        "department_id": department_id,
        "category": category,
        **parse_fields(fields),
    }
    return {key: value for key, value in values.items() if value is not None}


@changes.command("create")
@click.option("--subject", required=True)
@body_options
@click.option("--email", help="Requester email.")
@click.option("--requester-id", type=int)
@change_field_options
@pass_state
def changes_create(
    state: State,
    /,
    subject: str,
    body: str | None,
    body_file: Path | None,
    html: bool,
    email: str | None,
    requester_id: int | None,
    attachments: tuple[Path, ...],
    **options: Any,
) -> None:
    """Create a change (defaults: minor, low priority/impact/risk, open)."""
    description = read_body(body, body_file, html)
    assert description is not None
    values = _change_values(**options)
    if email is None and requester_id is None:
        email = state.client.settings.agent_email or state.client.settings.default_email
    emit(
        state,
        changes_api.create_change(
            state.client,
            subject=subject,
            description=description,
            email=email,
            requester_id=requester_id,
            attachments=attachments,
            **values,
        ),
    )


@changes.command("update")
@click.argument("change_id", type=int)
@click.option("--subject")
@body_options
@change_field_options
@pass_state
def changes_update(
    state: State,
    /,
    change_id: int,
    subject: str | None,
    body: str | None,
    body_file: Path | None,
    html: bool,
    attachments: tuple[Path, ...],
    **options: Any,
) -> None:
    """Change fields of a change and/or add attachments."""
    values = _change_values(**options)
    description = read_body(body, body_file, html, required=False)
    if description:
        values["description"] = description
    if subject:
        values["subject"] = subject
    if not values and not attachments:
        raise click.UsageError("Nothing to change")
    emit(state, changes_api.update_change(state.client, change_id, attachments=attachments, **values))


@changes.command("delete")
@click.argument("change_id", type=int)
@yes_option
@pass_state
def changes_delete(state: State, change_id: int, yes: bool) -> None:
    """Delete a change."""
    confirm(yes, f"change {change_id}")
    changes_api.delete_change(state.client, change_id)
    emit(state, {"deleted": change_id})


@changes.command("notes")
@click.argument("change_id", type=int)
@pass_state
def changes_notes(state: State, change_id: int) -> None:
    """Notes on a change."""
    emit(state, changes_api.list_change_notes(state.client, change_id))


@changes.command("note")
@click.argument("change_id", type=int)
@body_options
@click.option("--public", is_flag=True, help="Visible to requesters (default: private).")
@click.option("--notify", "notify_emails", multiple=True)
@attach_option
@pass_state
def changes_note(
    state: State,
    change_id: int,
    body: str | None,
    body_file: Path | None,
    html: bool,
    public: bool,
    notify_emails: tuple[str, ...],
    attachments: tuple[Path, ...],
) -> None:
    """Add a note. Attachments go on the change (notes can't hold files) and are linked."""
    text = read_body(body, body_file, html)
    assert text is not None
    emit(
        state,
        changes_api.add_change_note(
            state.client,
            change_id,
            text,
            private=not public,
            notify_emails=notify_emails,
            attachments=attachments,
        ),
    )


@changes.command("note-update")
@click.argument("change_id", type=int)
@click.argument("note_id", type=int)
@body_options
@pass_state
def changes_note_update(
    state: State, change_id: int, note_id: int, body: str | None, body_file: Path | None, html: bool
) -> None:
    """Replace a change note's body."""
    text = read_body(body, body_file, html)
    assert text is not None
    emit(state, changes_api.update_change_note(state.client, change_id, note_id, text))


@changes.command("note-delete")
@click.argument("change_id", type=int)
@click.argument("note_id", type=int)
@yes_option
@pass_state
def changes_note_delete(state: State, change_id: int, note_id: int, yes: bool) -> None:
    """Delete a change note."""
    confirm(yes, f"note {note_id} of change {change_id}")
    changes_api.delete_change_note(state.client, change_id, note_id)
    emit(state, {"deleted": note_id})


# -- solution articles ---------------------------------------------------------------------


@main.group()
def articles() -> None:
    """Knowledge base: categories, folders and solution articles."""


@articles.command("categories")
@pass_state
def articles_categories(state: State) -> None:
    """Solution categories."""
    emit(state, solutions_api.list_categories(state.client))


@articles.command("folders")
@click.option("--category-id", type=int, help="One category (default: all).")
@click.option("--top-level", is_flag=True, help="Without sub-folders.")
@pass_state
def articles_folders(state: State, category_id: int | None, top_level: bool) -> None:
    """Solution folders, sub-folders included."""
    emit(state, solutions_api.list_folders(state.client, category_id, recursive=not top_level))


@articles.command("list")
@click.option("--folder", "folder_id", type=int, help="Articles of one folder.")
@click.option("--mine", is_flag=True, help="Articles you created (FRESH_AGENT_ID).")
@click.option("--author", help="Articles created by this agent (email or id).")
@click.option("--all", "everything", is_flag=True, help="Every article.")
@click.option("--status", type=click.Choice(["draft", "published"]))
@pass_state
def articles_list(
    state: State, folder_id: int | None, mine: bool, author: str | None, everything: bool, status: str | None
) -> None:
    """List articles: --folder ID, --mine, --author or --all (the last three walk every folder)."""
    if sum((folder_id is not None, mine, author is not None, everything)) != 1:
        raise click.UsageError("Give exactly one of --folder, --mine, --author, --all")
    wanted = code(ArticleStatus, status) if status else None
    client = state.client
    if folder_id is not None:
        found = solutions_api.list_articles(client, folder_id)
    elif everything:
        found = list(solutions_api.iter_all_articles(client))
    elif author is not None and author.isdigit():
        found = solutions_api.list_my_articles(client, agent_id=int(author))
    else:
        found = solutions_api.list_my_articles(client, email=author)
    emit(state, [article for article in found if wanted is None or article.status == wanted])


@articles.command("search")
@click.argument("term")
@click.option("--user-email", help="Search as this user (needs permission to assume them).")
@pass_state
def articles_search(state: State, term: str, user_email: str | None) -> None:
    """Search published articles."""
    emit(state, solutions_api.search_articles(state.client, term, user_email=user_email))


@articles.command("get")
@click.argument("article_id", type=int)
@pass_state
def articles_get(state: State, article_id: int) -> None:
    """One article."""
    emit(state, solutions_api.get_article(state.client, article_id))


def article_options[G: Callable[..., Any]](f: G) -> G:
    for decorator in reversed(
        [
            click.option("--status", type=click.Choice(["draft", "published"])),
            click.option("--type", "article_type", type=click.Choice(["permanent", "workaround"])),
            click.option("--tag", "tags", multiple=True, help="Tag (repeatable; replaces on update)."),
            click.option("--keyword", "keywords", multiple=True, help="Keyword (repeatable)."),
            when_option("--review-date", help="When the article is due for review."),
            field_option,
            attach_option,
        ]
    ):
        f = decorator(f)
    return f


def _article_values(
    status: str | None,
    article_type: str | None,
    tags: Sequence[str],
    keywords: Sequence[str],
    review_date: date | None,
    fields: Sequence[str],
) -> dict[str, Any]:
    values: dict[str, Any] = {
        "status": code(ArticleStatus, status) if status else None,
        "article_type": code(ArticleType, article_type) if article_type else None,
        "tags": list(tags) or None,
        "keywords": list(keywords) or None,
        "review_date": review_date,
        **parse_fields(fields),
    }
    return {key: value for key, value in values.items() if value is not None}


@articles.command("create")
@click.option("--title", required=True)
@body_options
@click.option("--folder", "folder_id", type=int, required=True)
@article_options
@pass_state
def articles_create(
    state: State,
    /,
    title: str,
    body: str | None,
    body_file: Path | None,
    html: bool,
    folder_id: int,
    attachments: tuple[Path, ...],
    **options: Any,
) -> None:
    """Create an article (a draft unless --status published)."""
    description = read_body(body, body_file, html)
    assert description is not None
    values = _article_values(**options)
    emit(
        state,
        solutions_api.create_article(
            state.client,
            title=title,
            description=description,
            folder_id=folder_id,
            attachments=attachments,
            **values,
        ),
    )


@articles.command("update")
@click.argument("article_id", type=int)
@click.option("--title")
@body_options
@click.option("--folder", "folder_id", type=int, help="Move to this folder.")
@article_options
@pass_state
def articles_update(
    state: State,
    /,
    article_id: int,
    title: str | None,
    body: str | None,
    body_file: Path | None,
    html: bool,
    folder_id: int | None,
    attachments: tuple[Path, ...],
    **options: Any,
) -> None:
    """Change an article and/or add attachments."""
    values = _article_values(**options)
    description = read_body(body, body_file, html, required=False)
    for name, value in (("title", title), ("description", description), ("folder_id", folder_id)):
        if value is not None:
            values[name] = value
    if not values and not attachments:
        raise click.UsageError("Nothing to change")
    emit(state, solutions_api.update_article(state.client, article_id, attachments=attachments, **values))


@articles.command("publish")
@click.argument("article_id", type=int)
@pass_state
def articles_publish(state: State, article_id: int) -> None:
    """Publish an article."""
    emit(state, solutions_api.publish_article(state.client, article_id))


@articles.command("delete")
@click.argument("article_id", type=int)
@yes_option
@pass_state
def articles_delete(state: State, article_id: int, yes: bool) -> None:
    """Move an article to trash."""
    confirm(yes, f"article {article_id}")
    solutions_api.delete_article(state.client, article_id)
    emit(state, {"deleted": article_id})


# -- announcements -------------------------------------------------------------------------


@main.group()
def announcements() -> None:
    """Announcements."""


@announcements.command("list")
@click.option("--state", "announcement_state", type=click.Choice(list(announcements_api.STATES)))
@pass_state
def announcements_list(state: State, announcement_state: str | None) -> None:
    """List announcements."""
    emit(state, announcements_api.list_announcements(state.client, state=announcement_state))


@announcements.command("get")
@click.argument("announcement_id", type=int)
@pass_state
def announcements_get(state: State, announcement_id: int) -> None:
    """One announcement."""
    emit(state, announcements_api.get_announcement(state.client, announcement_id))


def announcement_options[G: Callable[..., Any]](f: G) -> G:
    for decorator in reversed(
        [
            when_option("--from", "visible_from", help="Visible from (default on create: now)."),
            when_option("--till", "visible_till", help="Visible until."),
            click.option("--visibility", type=click.Choice(list(announcements_api.VISIBILITIES))),
            click.option("--department", "departments", type=int, multiple=True),
            click.option("--group", "groups", type=int, multiple=True),
            click.option("--send-email/--no-send-email", default=None, help="Also send it by email."),
            click.option("--email", "additional_emails", multiple=True, help="Extra recipient (repeatable)."),
            field_option,
        ]
    ):
        f = decorator(f)
    return f


def _announcement_values(
    visible_from: date | None,
    visible_till: date | None,
    visibility: str | None,
    departments: Sequence[int],
    groups: Sequence[int],
    send_email: bool | None,
    additional_emails: Sequence[str],
    fields: Sequence[str],
) -> dict[str, Any]:
    def moment(value: date | None) -> datetime | None:
        if value is None or isinstance(value, datetime):
            return value
        return datetime.combine(value, datetime.min.time())

    values: dict[str, Any] = {
        "visible_from": moment(visible_from),
        "visible_till": moment(visible_till),
        "visibility": visibility,
        "departments": list(departments) or None,
        "groups": list(groups) or None,
        "send_email": send_email,
        "additional_emails": list(additional_emails) or None,
        **parse_fields(fields),
    }
    return {key: value for key, value in values.items() if value is not None}


@announcements.command("create")
@click.option("--title", required=True)
@body_options
@announcement_options
@pass_state
def announcements_create(
    state: State, /, title: str, body: str | None, body_file: Path | None, html: bool, **options: Any
) -> None:
    """Create an announcement."""
    text = read_body(body, body_file, html)
    assert text is not None
    emit(
        state,
        announcements_api.create_announcement(
            state.client, title=title, body_html=text, **_announcement_values(**options)
        ),
    )


@announcements.command("update")
@click.argument("announcement_id", type=int)
@click.option("--title")
@body_options
@announcement_options
@pass_state
def announcements_update(
    state: State,
    /,
    announcement_id: int,
    title: str | None,
    body: str | None,
    body_file: Path | None,
    html: bool,
    **options: Any,
) -> None:
    """Change an announcement."""
    values = _announcement_values(**options)
    text = read_body(body, body_file, html, required=False)
    if text:
        values["body_html"] = text
    if title:
        values["title"] = title
    if not values:
        raise click.UsageError("Nothing to change")
    emit(state, announcements_api.update_announcement(state.client, announcement_id, **values))


@announcements.command("delete")
@click.argument("announcement_id", type=int)
@yes_option
@pass_state
def announcements_delete(state: State, announcement_id: int, yes: bool) -> None:
    """Delete an announcement."""
    confirm(yes, f"announcement {announcement_id}")
    announcements_api.delete_announcement(state.client, announcement_id)
    emit(state, {"deleted": announcement_id})


# -- alerts ----------------------------------------------------------------------------------


@main.group()
def alerts() -> None:
    """Alert Management alerts (read)."""


@alerts.command("list")
@click.option("--query", help="Raw alert query (overrides the options below).")
@click.option("--severity", multiple=True, help="ok, warning, error, critical (repeatable).")
@click.option("--state", "alert_state", multiple=True, help="open, resolved, reopen (repeatable).")
@when_option("--since", help="Occurred at or after.")
@when_option("--until", help="Occurred at or before.")
@click.option("--integration-id", type=int)
@click.option("--order-by", type=click.Choice(["updated_at", "occurrence_time", "severity", "state", "id"]))
@click.option("--order-type", type=click.Choice(["asc", "desc"]))
@pass_state
def alerts_list(
    state: State,
    query: str | None,
    severity: tuple[str, ...],
    alert_state: tuple[str, ...],
    since: date | None,
    until: date | None,
    integration_id: int | None,
    order_by: str | None,
    order_type: str | None,
) -> None:
    """List alerts."""
    if query is None:
        query = alerts_api.alert_query(
            severity=severity, state=alert_state, since=since, until=until, integration_id=integration_id
        )
    emit(state, alerts_api.list_alerts(state.client, query, order_by=order_by, order_type=order_type))


@alerts.command("get")
@click.argument("alert_id", type=int)
@pass_state
def alerts_get(state: State, alert_id: int) -> None:
    """One alert."""
    emit(state, alerts_api.get_alert(state.client, alert_id))


@alerts.command("notes")
@click.argument("alert_id", type=int)
@pass_state
def alerts_notes(state: State, alert_id: int) -> None:
    """Notes on an alert."""
    emit(state, alerts_api.list_alert_notes(state.client, alert_id))


# -- org --------------------------------------------------------------------------------------


@main.group()
def org() -> None:
    """Agents, groups, roles, departments, locations."""


@org.command("agents")
@click.option("--email")
@click.option("--active/--inactive", default=None)
@pass_state
def org_agents(state: State, email: str | None, active: bool | None) -> None:
    """List agents."""
    emit(state, org_api.list_agents(state.client, email=email, active=active))


@org.command("me")
@pass_state
def org_me(state: State) -> None:
    """The agent FRESH_AGENT_ID (or FRESH_AGENT_EMAIL) names."""
    settings = state.client.settings
    if settings.agent_id is not None:
        emit(state, org_api.get_agent(state.client, settings.agent_id))
    elif settings.agent_email:
        emit(state, org_api.resolve_agent(state.client, settings.agent_email))
    else:
        raise click.UsageError("Set FRESH_AGENT_ID or FRESH_AGENT_EMAIL")


@org.command("groups")
@pass_state
def org_groups(state: State) -> None:
    """List agent groups."""
    emit(state, org_api.list_groups(state.client))


@org.command("roles")
@pass_state
def org_roles(state: State) -> None:
    """List agent roles."""
    emit(state, org_api.list_roles(state.client))


@org.command("departments")
@pass_state
def org_departments(state: State) -> None:
    """List departments."""
    emit(state, org_api.list_departments(state.client))


@org.command("locations")
@pass_state
def org_locations(state: State) -> None:
    """List locations."""
    emit(state, org_api.list_locations(state.client))


# -- assets -----------------------------------------------------------------------------------


@main.group()
def assets() -> None:
    """Assets, asset types, devices, and product/vendor assignment."""


type_option = click.option(
    "--type", "types", multiple=True, help="Asset type name or id; child types included (repeatable)."
)


@assets.command("list")
@type_option
@click.option(
    "--filter", "filter_query", help="API filter query, e.g. \"asset_state:'IN USE'\" (max 1200 results)."
)
@click.option("--search", help="API search, e.g. \"name:'fw-'\".")
@click.option(
    "--type-fields", is_flag=True, help="Include type-specific fields (product, vendor, state, ...)."
)
@click.option("--trashed", is_flag=True)
@pass_state
def assets_list(
    state: State,
    types: tuple[str, ...],
    filter_query: str | None,
    search: str | None,
    type_fields: bool,
    trashed: bool,
) -> None:
    """List assets."""
    emit(
        state,
        assets_api.list_assets(
            state.client,
            types=list(types) or None,
            include_type_fields=type_fields,
            filter=filter_query,
            search=search,
            trashed=trashed,
        ),
    )


@assets.command("get")
@click.argument("display_id", type=int)
@pass_state
def assets_get(state: State, display_id: int) -> None:
    """One asset, with type fields."""
    emit(state, assets_api.get_asset(state.client, display_id))


@assets.command("types")
@pass_state
def assets_types(state: State) -> None:
    """Asset types, with their path in the type tree."""
    tree = assets_api.asset_type_tree(state.client)
    emit(
        state,
        sorted(
            (
                {
                    "id": t.id,
                    "name": t.name,
                    "parent_asset_type_id": t.parent_asset_type_id,
                    "path": tree.path(t.id),
                }
                for t in tree.types.values()
            ),
            key=lambda row: str(row["path"]).casefold(),
        ),
    )


@assets.command("fields")
@click.argument("asset_type")
@pass_state
def assets_fields(state: State, asset_type: str) -> None:
    """The fields of an asset type (name or id), and which are product/vendor/state."""
    tree = assets_api.asset_type_tree(state.client)
    type_id = tree.resolve(asset_type).id
    emit(
        state,
        {
            "asset_type": {"id": type_id, "path": tree.path(type_id)},
            "product_vendor_fields": assets_api.product_vendor_fields(state.client, type_id),
            "fields": assets_api.asset_type_fields(state.client, type_id),
        },
    )


@assets.command("devices")
@type_option
@pass_state
def assets_devices(state: State, types: tuple[str, ...]) -> None:
    """Assets of the device types (FRESH_DEVICE_ASSET_TYPES unless --type)."""
    emit(state, assets_api.list_devices(state.client, types=list(types) or None))


@assets.command("update")
@click.argument("display_id", type=int)
@field_option
@pass_state
def assets_update(state: State, display_id: int, fields: tuple[str, ...]) -> None:
    """Change asset fields (type fields as --field type_fields.product_636=12)."""
    values = parse_fields(fields)
    if not values:
        raise click.UsageError("Nothing to change: give --field NAME=VALUE")
    emit(state, assets_api.update_asset(state.client, display_id, **values))


@assets.command("tag-unknown")
@click.option("--type", "types", multiple=True, required=True, help="Asset type (repeatable), e.g. Firewall.")
@click.option("--only", type=click.Choice(["product", "vendor"]), help="Fill only this field.")
@click.option("--label", help="Placeholder name (default FRESH_UNKNOWN_LABEL, 'Unknown').")
@apply_option
@pass_state
def assets_tag_unknown(
    state: State, types: tuple[str, ...], only: str | None, label: str | None, apply: bool
) -> None:
    """Give assets with no product/vendor the 'Unknown' placeholders, to filter and fix later."""
    plan = maintenance.plan_tag_unknown(
        state.client, list(types), label=label, fill=(only,) if only else ("product", "vendor")
    )
    run_plan(state, plan, apply)


@assets.command("set-product-vendor")
@click.option("--id", "display_ids", type=int, multiple=True, help="Asset display id (repeatable).")
@type_option
@click.option("--name", help="Asset name substring or glob.")
@click.option("--current-product", help="Only assets whose product matches (glob, or 'none').")
@click.option("--current-vendor", help="Only assets whose vendor matches (glob, or 'none').")
@click.option("--product", help="Product name to set.")
@click.option("--vendor", help="Vendor name to set.")
@click.option("--manufacturer", help="Manufacturer for products created with --create-missing.")
@click.option("--create-missing", is_flag=True, help="Create the product/vendor if it doesn't exist.")
@apply_option
@pass_state
def assets_set_product_vendor(
    state: State,
    display_ids: tuple[int, ...],
    types: tuple[str, ...],
    name: str | None,
    current_product: str | None,
    current_vendor: str | None,
    product: str | None,
    vendor: str | None,
    manufacturer: str | None,
    create_missing: bool,
    apply: bool,
) -> None:
    """Set product and/or vendor on selected assets, e.g. --current-vendor Unknown --name 'fw-*'."""
    plan = maintenance.plan_set_product_vendor(
        state.client,
        product=product,
        vendor=vendor,
        display_ids=display_ids,
        types=types,
        name=name,
        current_product=current_product,
        current_vendor=current_vendor,
        manufacturer=manufacturer,
        create_missing=create_missing,
    )
    run_plan(state, plan, apply)


# -- products and vendors ------------------------------------------------------------------


@main.group()
def products() -> None:
    """Products (asset models)."""


@products.command("list")
@click.option("--type", "asset_type", help="Only products of this asset type (name or id).")
@click.option("--name", help="Name substring or glob.")
@pass_state
def products_list(state: State, asset_type: str | None, name: str | None) -> None:
    """List products."""
    type_id = assets_api.asset_type_tree(state.client).resolve(asset_type).id if asset_type else None
    emit(state, catalog_api.list_products(state.client, asset_type_id=type_id, name=name))


@products.command("get")
@click.argument("product_id", type=int)
@pass_state
def products_get(state: State, product_id: int) -> None:
    """One product."""
    emit(state, catalog_api.get_product(state.client, product_id))


@products.command("create")
@click.option("--name", required=True)
@click.option("--type", "asset_type", required=True, help="Asset type name or id.")
@click.option("--manufacturer")
@field_option
@pass_state
def products_create(
    state: State, name: str, asset_type: str, manufacturer: str | None, fields: tuple[str, ...]
) -> None:
    """Create a product."""
    type_id = assets_api.asset_type_tree(state.client).resolve(asset_type).id
    emit(
        state,
        catalog_api.create_product(
            state.client, name=name, asset_type_id=type_id, manufacturer=manufacturer, **parse_fields(fields)
        ),
    )


@products.command("update")
@click.argument("product_id", type=int)
@field_option
@pass_state
def products_update(state: State, product_id: int, fields: tuple[str, ...]) -> None:
    """Change a product."""
    emit(state, catalog_api.update_product(state.client, product_id, **parse_fields(fields)))


@products.command("delete")
@click.argument("product_id", type=int)
@yes_option
@pass_state
def products_delete(state: State, product_id: int, yes: bool) -> None:
    """Delete a product."""
    confirm(yes, f"product {product_id}")
    catalog_api.delete_product(state.client, product_id)
    emit(state, {"deleted": product_id})


@main.group()
def vendors() -> None:
    """Vendors, and merging duplicates."""


@vendors.command("list")
@click.option("--name", help="Name substring or glob.")
@pass_state
def vendors_list(state: State, name: str | None) -> None:
    """List vendors."""
    emit(state, catalog_api.list_vendors(state.client, name=name))


@vendors.command("get")
@click.argument("vendor_id", type=int)
@pass_state
def vendors_get(state: State, vendor_id: int) -> None:
    """One vendor."""
    emit(state, catalog_api.get_vendor(state.client, vendor_id))


@vendors.command("create")
@click.option("--name", required=True)
@field_option
@pass_state
def vendors_create(state: State, name: str, fields: tuple[str, ...]) -> None:
    """Create a vendor."""
    emit(state, catalog_api.create_vendor(state.client, name=name, **parse_fields(fields)))


@vendors.command("update")
@click.argument("vendor_id", type=int)
@field_option
@pass_state
def vendors_update(state: State, vendor_id: int, fields: tuple[str, ...]) -> None:
    """Change a vendor."""
    emit(state, catalog_api.update_vendor(state.client, vendor_id, **parse_fields(fields)))


@vendors.command("delete")
@click.argument("vendor_id", type=int)
@yes_option
@pass_state
def vendors_delete(state: State, vendor_id: int, yes: bool) -> None:
    """Delete a vendor."""
    confirm(yes, f"vendor {vendor_id}")
    catalog_api.delete_vendor(state.client, vendor_id)
    emit(state, {"deleted": vendor_id})


@vendors.command("merge")
@click.argument("sources", nargs=-1, required=True)
@click.option("--into", required=True, help="The vendor to keep (exact name or id).")
@click.option("--delete", is_flag=True, help="Delete the source vendors afterwards (default: keep them).")
@click.option("--skip-assets", is_flag=True, help="Don't scan assets (saves a full asset listing).")
@click.option("--skip-contracts", is_flag=True)
@apply_option
@pass_state
def vendors_merge(
    state: State,
    sources: tuple[str, ...],
    into: str,
    delete: bool,
    skip_assets: bool,
    skip_contracts: bool,
    apply: bool,
) -> None:
    """Re-point software, assets and contracts from SOURCES (names, globs or ids) to --into."""
    plan = maintenance.plan_vendor_merge(
        state.client,
        list(sources),
        into,
        delete=delete,
        include_assets=not skip_assets,
        include_contracts=not skip_contracts,
    )
    run_plan(state, plan, apply)


# -- software ---------------------------------------------------------------------------------


@main.group()
def software() -> None:
    """Software: entries, installations, where-installed, merges and cleanup."""


@software.command("list")
@click.option("--vendor", help="Vendor name substring or glob.")
@click.option("--name", help="Name substring or glob.")
@click.option("--status", help="managed, ignored, blacklisted or none.")
@pass_state
def software_list(state: State, vendor: str | None, name: str | None, status: str | None) -> None:
    """List software entries."""
    emit(state, software_api.list_software(state.client, vendor=vendor, name=name, status=status))


@software.command("get")
@click.argument("software_id", type=int)
@pass_state
def software_get(state: State, software_id: int) -> None:
    """One software entry."""
    emit(state, software_api.get_software(state.client, software_id))


@software.command("installations")
@click.argument("software_id", type=int)
@pass_state
def software_installations(state: State, software_id: int) -> None:
    """Installations of one software entry."""
    emit(state, software_api.list_installations(state.client, software_id))


@software.command("users")
@click.argument("software_id", type=int)
@pass_state
def software_users(state: State, software_id: int) -> None:
    """Users of one software entry."""
    emit(state, software_api.list_software_users(state.client, software_id))


@software.command("licenses")
@click.argument("software_id", type=int)
@pass_state
def software_licenses(state: State, software_id: int) -> None:
    """Licenses of one software entry."""
    emit(state, software_api.list_licenses(state.client, software_id))


@software.command("update")
@click.argument("software_id", type=int)
@click.option("--vendor-id", type=int, help="New publisher (vendor id).")
@click.option("--status", type=click.Choice(["managed", "ignored", "blacklisted"]))
@field_option
@pass_state
def software_update(
    state: State, software_id: int, vendor_id: int | None, status: str | None, fields: tuple[str, ...]
) -> None:
    """Change a software entry."""
    values = parse_fields(fields)
    if vendor_id is not None:
        values["publisher_id"] = vendor_id
    if status:
        values["status"] = status
    if not values:
        raise click.UsageError("Nothing to change")
    emit(state, software_api.update_software(state.client, software_id, **values))


@software.command("where")
@click.option("--vendor", help="Vendor name substring or glob.")
@click.option("--name", help="Software name substring or glob.")
@click.option("--version", help='Exact, glob ("24.*") or range ("<24.09").')
@click.option("--summary", is_flag=True, help="Per software: device counts per version.")
@click.option("--by-device", is_flag=True, help="Grouped per device.")
@pass_state
def software_where(
    state: State, vendor: str | None, name: str | None, version: str | None, summary: bool, by_device: bool
) -> None:
    """Every device with the matching software installed."""
    if not (vendor or name):
        raise click.UsageError("Give --vendor and/or --name (all software would be one call per entry)")
    if summary and by_device:
        raise click.UsageError("--summary or --by-device, not both")
    installs = software_api.where_installed(state.client, vendor=vendor, name=name, version=version)
    if summary:
        emit(state, software_api.summarise_installs(installs))
    elif by_device:
        emit(state, software_api.by_device(installs))
    else:
        emit(state, installs)


@software.command("merge")
@click.argument("sources", nargs=-1, required=True)
@click.option("--vendor", required=True, help="The vendor all sources and the target belong to.")
@click.option("--into", required=True, help="The entry to keep (exact name or id); created if missing.")
@click.option("--delete", is_flag=True, help="Delete the sources (default: mark them ignored).")
@apply_option
@pass_state
def software_merge(
    state: State, sources: tuple[str, ...], vendor: str, into: str, delete: bool, apply: bool
) -> None:
    """Move installations and users of SOURCES (names, globs or ids) into --into, within one vendor."""
    plan = maintenance.plan_software_merge(state.client, vendor, list(sources), into, delete=delete)
    run_plan(state, plan, apply)


@software.command("delete")
@click.option("--vendor", help="Vendor name substring or glob.")
@click.option("--name", help="Software name substring or glob.")
@click.option("--id", "ids", type=int, multiple=True)
@click.option("--status", help="managed, ignored, blacklisted or none.")
@click.option(
    "--include-used", is_flag=True, help="Also delete entries with installations, users or licenses."
)
@apply_option
@pass_state
def software_delete(
    state: State,
    vendor: str | None,
    name: str | None,
    ids: tuple[int, ...],
    status: str | None,
    include_used: bool,
    apply: bool,
) -> None:
    """Delete software entries (only unused ones unless --include-used)."""
    plan = maintenance.plan_software_delete(
        state.client, vendor=vendor, name=name, ids=ids, status=status, include_used=include_used
    )
    run_plan(state, plan, apply)


if __name__ == "__main__":  # pragma: no cover
    main()
