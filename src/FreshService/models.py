"""Records returned by the library.

Field names are the API's own (Freshservice is snake_case already), so a record can be
traced back to the JSON it came from. `model.model_dump(mode="json")` gives a JSON-ready
dict. Unknown API fields are ignored; for anything not modelled, use client.get().

Numeric codes (status, priority, ...) stay plain ints on the records, because accounts
can add their own values. The IntEnums below name the built-in ones for callers.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import IntEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def as_datetime(value: Any) -> datetime | None:
    """ISO-8601 timestamps; "" and unparseable values (the API has served "200-01-20") -> None."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


Timestamp = Annotated[datetime | None, BeforeValidator(as_datetime)]


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


# -- codes -------------------------------------------------------------------------


class TicketStatus(IntEnum):
    OPEN = 2
    PENDING = 3
    RESOLVED = 4
    CLOSED = 5


class Priority(IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    URGENT = 4


class Level(IntEnum):
    """Ticket urgency and impact; change impact."""

    LOW = 1
    MEDIUM = 2
    HIGH = 3


class TicketSource(IntEnum):
    EMAIL = 1
    PORTAL = 2
    PHONE = 3
    CHAT = 4
    FEEDBACK_WIDGET = 5
    YAMMER = 6
    AWS_CLOUDWATCH = 7
    PAGERDUTY = 8
    WALKUP = 9
    SLACK = 10


class ChangeStatus(IntEnum):
    OPEN = 1
    PLANNING = 2
    APPROVAL = 3
    PENDING_RELEASE = 4
    PENDING_REVIEW = 5
    CLOSED = 6


class ChangeType(IntEnum):
    MINOR = 1
    STANDARD = 2
    MAJOR = 3
    EMERGENCY = 4


class Risk(IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    VERY_HIGH = 4


class ArticleStatus(IntEnum):
    DRAFT = 1
    PUBLISHED = 2


class ArticleType(IntEnum):
    PERMANENT = 1
    WORKAROUND = 2


class AlertSeverity(IntEnum):
    OK = 51
    WARNING = 101
    ERROR = 151
    CRITICAL = 201


class AlertState(IntEnum):
    OPEN = 1
    RESOLVED = 2
    REOPEN = 3


class RequestedItemStage(IntEnum):
    REQUESTED = 1
    DELIVERED = 2
    CANCELLED = 3
    FULFILLED = 4
    PARTIALLY_FULFILLED = 5


def code(enum: type[IntEnum], name_or_value: str | int) -> int:
    """'high' / 'HIGH' / 'very-high' / 3 -> the enum's int value."""
    if isinstance(name_or_value, int):
        return int(name_or_value)
    text = name_or_value.strip()
    if text.isdigit():
        return int(text)
    key = re.sub(r"[\s-]+", "_", text).upper()
    try:
        return int(enum[key])
    except KeyError:
        names = ", ".join(member.name.lower() for member in enum)
        raise ValueError(f"{name_or_value!r} is not one of: {names} (or a number)") from None


# -- tickets, conversations, service requests -------------------------------------


class Attachment(ApiModel):
    id: int | None = None
    name: str | None = None
    content_type: str | None = None
    size: int | None = None
    attachment_url: str | None = None
    created_at: Timestamp = None
    updated_at: Timestamp = None


class Conversation(ApiModel):
    """A reply or a note on a ticket (or a service request, which is a ticket)."""

    id: int
    ticket_id: int | None = None
    body: str | None = None
    body_text: str | None = None
    incoming: bool | None = None
    private: bool | None = None
    source: int | None = None
    """0 email, 1 form, 2 note, 3 status, 4 meta, 5 feedback, 6 forward."""
    user_id: int | None = None
    support_email: str | None = None
    from_email: str | None = None
    to_emails: list[str] | None = None
    cc_emails: list[str] | None = None
    bcc_emails: list[str] | None = None
    attachments: list[Attachment] = []
    created_at: Timestamp = None
    updated_at: Timestamp = None


class Ticket(ApiModel):
    """An incident or a service request (type "Service Request")."""

    id: int
    workspace_id: int | None = None
    type: str | None = None
    subject: str | None = None
    description: str | None = None
    description_text: str | None = None
    status: int | None = None
    priority: int | None = None
    urgency: int | None = None
    impact: int | None = None
    source: int | None = None
    category: str | None = None
    sub_category: str | None = None
    item_category: str | None = None
    requester_id: int | None = None
    requested_for_id: int | None = None
    responder_id: int | None = None
    group_id: int | None = None
    department_id: int | None = None
    email_config_id: int | None = None
    cc_emails: list[str] | None = None
    fwd_emails: list[str] | None = None
    reply_cc_emails: list[str] | None = None
    to_emails: list[str] | None = None
    tags: list[str] | None = None
    spam: bool | None = None
    deleted: bool | None = None
    is_escalated: bool | None = None
    fr_escalated: bool | None = None
    due_by: Timestamp = None
    fr_due_by: Timestamp = None
    approval_status: int | None = None
    approval_status_name: str | None = None
    resolution_notes: str | None = None
    custom_fields: dict[str, Any] = {}
    attachments: list[Attachment] = []
    conversations: list[Conversation] | None = None
    """Only with include=conversations (the first 10)."""
    created_at: Timestamp = None
    updated_at: Timestamp = None


class RequestedItem(ApiModel):
    id: int
    service_item_id: int | None = None
    service_item_name: str | None = None
    quantity: int | None = None
    stage: int | None = None
    loaned: bool | None = None
    cost_per_request: float | None = None
    remarks: str | None = None
    delivery_time: float | None = None
    is_parent: bool | None = None
    custom_fields: dict[str, Any] = {}
    created_at: Timestamp = None
    updated_at: Timestamp = None


class CatalogCategory(ApiModel):
    id: int
    name: str = ""
    description: str | None = None
    position: int | None = None
    workspace_id: int | None = None
    created_at: Timestamp = None
    updated_at: Timestamp = None


class CatalogItem(ApiModel):
    """A service catalog item. Place a request for it with create_service_request()."""

    id: int
    display_id: int | None = None
    name: str = ""
    short_description: str | None = None
    description: str | None = None
    category_id: int | None = None
    product_id: int | None = None
    item_type: int | None = None
    ci_type_id: int | None = None
    visibility: int | None = None
    deleted: bool | None = None
    create_child: bool | None = None
    cost_visibility: bool | None = None
    delivery_time_visibility: bool | None = None
    cost: str | float | None = None
    delivery_time: float | None = None
    workspace_id: int | None = None
    custom_fields: list[dict[str, Any]] = []
    """The item's form fields (name, label, field_type, required, choices)."""
    created_at: Timestamp = None
    updated_at: Timestamp = None


# -- changes -----------------------------------------------------------------------


class Change(ApiModel):
    id: int
    workspace_id: int | None = None
    subject: str | None = None
    description: str | None = None
    description_text: str | None = None
    requester_id: int | None = None
    agent_id: int | None = None
    group_id: int | None = None
    department_id: int | None = None
    priority: int | None = None
    impact: int | None = None
    status: int | None = None
    risk: int | None = None
    change_type: int | None = None
    approval_status: int | None = None
    planned_start_date: Timestamp = None
    planned_end_date: Timestamp = None
    category: str | None = None
    sub_category: str | None = None
    item_category: str | None = None
    custom_fields: dict[str, Any] = {}
    planning_fields: dict[str, Any] = {}
    """reason_for_change, change_impact, rollout_plan, backout_plan: {description, ...}."""
    maintenance_window: dict[str, Any] | None = None
    blackout_window: dict[str, Any] | None = None
    assets: list[dict[str, Any]] = []
    impacted_services: list[dict[str, Any]] = []
    attachments: list[Attachment] = []
    created_at: Timestamp = None
    updated_at: Timestamp = None


class Note(ApiModel):
    """A note on a change. (Ticket notes are Conversations.)"""

    id: int
    user_id: int | None = None
    body: str | None = None
    body_text: str | None = None
    private: bool | None = None
    notify_emails: list[str] = []
    attachments: list[Attachment] = []
    created_at: Timestamp = None
    updated_at: Timestamp = None


# -- knowledge base ----------------------------------------------------------------


class SolutionCategory(ApiModel):
    id: int
    name: str = ""
    description: str | None = None
    position: int | None = None
    default_category: bool | None = None
    visible_in_portals: list[int] | None = None
    workspace_id: int | None = None
    created_at: Timestamp = None
    updated_at: Timestamp = None


class SolutionFolder(ApiModel):
    id: int
    name: str = ""
    description: str | None = None
    category_id: int | None = None
    parent_id: int | None = None
    """Set on sub-folders (from list_folders(recursive=True))."""
    position: int | None = None
    visibility: int | None = None
    default_folder: bool | None = None
    approval_settings: dict[str, Any] | None = None
    workspace_id: int | None = None
    created_at: Timestamp = None
    updated_at: Timestamp = None


class Article(ApiModel):
    id: int
    title: str = ""
    description: str | None = None
    description_text: str | None = None
    folder_id: int | None = None
    category_id: int | None = None
    status: int | None = None
    """1 draft, 2 published."""
    approval_status: int | None = None
    article_type: int | None = None
    """1 permanent, 2 workaround."""
    agent_id: int | None = None
    """The agent who created the article - the only notion of "owner" the API has."""
    modified_by: int | None = None
    modified_at: Timestamp = None
    tags: list[str] = []
    keywords: list[str] = []
    url: str | None = None
    review_date: Timestamp = None
    views: int | None = None
    thumbs_up: int | None = None
    thumbs_down: int | None = None
    inserted_into_tickets: int | None = None
    position: int | None = None
    deleted: bool | None = None
    workspace_id: int | None = None
    attachments: list[Attachment] = []
    created_at: Timestamp = None
    updated_at: Timestamp = None


# -- announcements, alerts ---------------------------------------------------------


class Announcement(ApiModel):
    id: int
    title: str = ""
    body: str | None = None
    body_html: str | None = None
    state: str | None = None
    """active, archived or scheduled."""
    visibility: str | None = None
    """everyone, agents_only or agents_and_groups."""
    visible_from: Timestamp = None
    visible_till: Timestamp = None
    departments: list[int] = []
    groups: list[int] = []
    is_read: bool | None = None
    send_email: bool | None = None
    additional_emails: list[str] = []
    created_by: int | None = None
    workspace_id: int | None = None
    created_at: Timestamp = None
    updated_at: Timestamp = None


class Alert(ApiModel):
    id: int
    workspace_id: int | None = None
    subject: str | None = None
    description: str | None = None
    resource: str | None = None
    severity: int | None = None
    """51 ok, 101 warning, 151 error, 201 critical."""
    state: int | None = None
    """1 open, 2 resolved, 3 reopen."""
    tags: list[str] | str | None = None
    integration_id: int | None = None
    integration_name: str | None = None
    metric_name: str | None = None
    metric_value: str | None = None
    node: str | None = None
    incident_id: int | None = None
    acknowledged_by_id: int | None = None
    acknowledged_at: Timestamp = None
    archived: bool | None = None
    occurrence_time: Timestamp = None
    additional_info: dict[str, Any] | None = None
    created_at: Timestamp = None
    updated_at: Timestamp = None


class AlertNote(ApiModel):
    id: int
    alert_id: int | None = None
    user_id: int | None = None
    description: str | None = None
    workspace_id: int | None = None
    created_at: Timestamp = None
    updated_at: Timestamp = None


# -- people and organisation -------------------------------------------------------


class Agent(ApiModel):
    id: int
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    active: bool | None = None
    occasional: bool | None = None
    job_title: str | None = None
    work_phone_number: str | None = None
    mobile_phone_number: str | None = None
    department_ids: list[int] = []
    location_id: int | None = None
    reporting_manager_id: int | None = None
    time_zone: str | None = None
    language: str | None = None
    roles: list[dict[str, Any]] = []
    member_of: list[int] = []
    observer_of: list[int] = []
    last_login_at: Timestamp = None
    last_active_at: Timestamp = None
    created_at: Timestamp = None
    updated_at: Timestamp = None

    @property
    def name(self) -> str:
        return " ".join(part for part in (self.first_name, self.last_name) if part)


class Group(ApiModel):
    """An agent group."""

    id: int
    name: str = ""
    description: str | None = None
    members: list[int] = []
    observers: list[int] = []
    leaders: list[int] = []
    escalate_to: int | None = None
    unassigned_for: str | None = None
    business_hours_id: int | None = None
    restricted: bool | None = None
    approval_required: bool | None = None
    auto_ticket_assign: bool | None = None
    workspace_id: int | None = None
    created_at: Timestamp = None
    updated_at: Timestamp = None


class Role(ApiModel):
    """An agent role (read-only in the API)."""

    id: int
    name: str = ""
    description: str | None = None
    default: bool | None = None
    role_type: int | None = None
    scopes: dict[str, Any] | None = None
    created_at: Timestamp = None
    updated_at: Timestamp = None


class Department(ApiModel):
    id: int
    name: str = ""
    description: str | None = None
    head_user_id: int | None = None
    prime_user_id: int | None = None
    domains: list[str] = []
    custom_fields: dict[str, Any] = {}
    workspace_id: int | None = None
    created_at: Timestamp = None
    updated_at: Timestamp = None


class Location(ApiModel):
    id: int
    name: str = ""
    parent_location_id: int | None = None
    primary_contact_id: int | None = None
    address: dict[str, Any] | None = None
    created_at: Timestamp = None
    updated_at: Timestamp = None


# -- assets, products, vendors -----------------------------------------------------

_TYPE_FIELD = re.compile(r"^(?P<base>[a-z_]+?)_(?P<type_id>\d+)$")


class AssetType(ApiModel):
    id: int
    name: str = ""
    parent_asset_type_id: int | None = None
    description: str | None = None
    visible: bool | None = None
    created_at: Timestamp = None
    updated_at: Timestamp = None


class AssetTypeField(ApiModel):
    """One field of an asset type's form. Type-specific fields are named `<base>_<type id>`,
    e.g. product_636, where 636 is the asset type that defines the field."""

    id: int | None = None
    name: str
    label: str | None = None
    field_type: str | None = None
    data_type: str | None = None
    required: bool | None = None
    default_field: bool | None = None
    asset_type_id: int | None = None
    field_header: str | None = None
    """The form section ("General", "Hardware", ...)."""
    choices: list[Any] | None = None

    @property
    def base_name(self) -> str:
        """product_636 -> product; name -> name."""
        match = _TYPE_FIELD.match(self.name)
        return match.group("base") if match else self.name


def type_field(type_fields: dict[str, Any], base: str) -> tuple[str | None, Any]:
    """The (name, value) of the type field `<base>_<digits>`, e.g. base "product"."""
    for name, value in type_fields.items():
        match = _TYPE_FIELD.match(name)
        if match and match.group("base") == base:
            return name, value
    return None, None


class Asset(ApiModel):
    id: int
    display_id: int
    """What every /assets/{id} path and installation_machine_id refers to."""
    name: str = ""
    description: str | None = None
    asset_type_id: int | None = None
    asset_tag: str | None = None
    impact: str | None = None
    usage_type: str | None = None
    author_type: str | None = None
    user_id: int | None = None
    agent_id: int | None = None
    group_id: int | None = None
    department_id: int | None = None
    location_id: int | None = None
    assigned_on: Timestamp = None
    workspace_id: int | None = None
    created_by_source: str | None = None
    last_updated_by_source: str | None = None
    sources: list[str] | None = None
    type_fields: dict[str, Any] = {}
    """Only with include=type_fields."""
    created_at: Timestamp = None
    updated_at: Timestamp = None

    @property
    def product_id(self) -> int | None:
        return _int(type_field(self.type_fields, "product")[1])

    @property
    def vendor_id(self) -> int | None:
        return _int(type_field(self.type_fields, "vendor")[1])

    @property
    def asset_state(self) -> str | None:
        value = type_field(self.type_fields, "asset_state")[1]
        return str(value) if value not in (None, "") else None

    @property
    def serial_number(self) -> str | None:
        value = type_field(self.type_fields, "serial_number")[1]
        return str(value) if value not in (None, "") else None


def _int(value: Any) -> int | None:
    if value in (None, "", 0, "0"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class Product(ApiModel):
    """A model of asset (e.g. "FortiGate 100F"). Belongs to one asset type; has no vendor link,
    only a free-text manufacturer."""

    id: int
    name: str = ""
    asset_type_id: int | None = None
    manufacturer: str | None = None
    status: str | None = None
    mode_of_procurement: str | None = None
    depreciation_type_id: int | None = None
    description: str | None = None
    description_text: str | None = None
    created_at: Timestamp = None
    updated_at: Timestamp = None


class Vendor(ApiModel):
    """A vendor: the publisher of software (publisher_id) and the vendor field on assets."""

    id: int
    name: str = ""
    description: str | None = None
    primary_contact_id: int | None = None
    address: dict[str, Any] | None = None
    line1: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    zipcode: str | None = None
    created_at: Timestamp = None
    updated_at: Timestamp = None


class Contract(ApiModel):
    id: int
    name: str | None = None
    contract_number: str | None = None
    contract_type_id: int | None = None
    vendor_id: int | None = None
    status: str | None = None
    start_date: Timestamp = None
    end_date: Timestamp = None
    workspace_id: int | None = None


# -- software ----------------------------------------------------------------------


class Software(ApiModel):
    """A software entry (/applications). Discovery creates one per distinct name."""

    id: int
    name: str = ""
    description: str | None = None
    application_type: str | None = None
    """desktop, saas or mobile."""
    status: str | None = None
    """managed, ignored, blacklisted (or empty: not yet triaged)."""
    publisher_id: int | None = None
    """The vendor id."""
    managed_by_id: int | None = None
    category: str | None = None
    notes: str | None = None
    source: str | None = None
    user_count: int | None = None
    installation_count: int | None = None
    workspace_id: int | None = None
    created_at: Timestamp = None
    updated_at: Timestamp = None


class Installation(ApiModel):
    id: int | None = None
    installation_machine_id: int
    """The asset's display_id."""
    installation_path: str | None = None
    version: str | None = None
    user_id: int | None = None
    department_id: int | None = None
    installation_date: Timestamp = None
    created_at: Timestamp = None
    updated_at: Timestamp = None


class SoftwareUser(ApiModel):
    id: int | None = None
    user_id: int
    license_id: int | None = None
    allocated_date: Timestamp = None
    first_used: Timestamp = None
    last_used: Timestamp = None
    source: str | None = None
    created_at: Timestamp = None
    updated_at: Timestamp = None


class SoftwareLicense(ApiModel):
    id: int
    contract_id: str | None = None
    """The contract number, e.g. CNTR-22."""
    created_time: Timestamp = None
    updated_time: Timestamp = None


class DeviceInstall(BaseModel):
    """One installation of a software on a device, joined with the software, vendor and asset."""

    software_id: int
    software_name: str
    vendor_id: int | None = None
    vendor_name: str | None = None
    version: str | None = None
    installation_path: str | None = None
    installation_date: datetime | None = None
    device_id: int
    """The asset's display_id."""
    device_name: str | None = None
    device_state: str | None = None
    asset_type_id: int | None = None
    asset_type_name: str | None = None
    user_id: int | None = None
    department_id: int | None = None
    url: str | None = None


class InstallSummary(BaseModel):
    """One software: on how many devices, and how many devices per version."""

    software_id: int
    software_name: str
    vendor_name: str | None = None
    device_count: int
    versions: dict[str, int]


class DeviceSoftware(BaseModel):
    """One device and everything (matching) installed on it."""

    device_id: int
    device_name: str | None = None
    device_state: str | None = None
    asset_type_name: str | None = None
    url: str | None = None
    software: list[dict[str, Any]] = Field(default_factory=list)
    """[{software_id, software_name, vendor_name, version}]"""


# -- maintenance plans -------------------------------------------------------------

ActionStatus = Literal["planned", "done", "failed", "skipped"]


class PlannedAction(BaseModel):
    """One write a maintenance plan will make.

    Paths and bodies may hold `<ref:NAME>` placeholders for the id of an object an earlier
    action creates (its `ref`). `requires` lists the indexes of actions that must succeed
    first; if one didn't, this action is skipped.
    """

    action: str
    target: str
    method: Literal["POST", "PUT", "DELETE"]
    path: str
    body: dict[str, Any] | None = None
    params: dict[str, Any] | None = None
    response_key: str | None = None
    ref: str | None = None
    requires: list[int] = []
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    status: ActionStatus = "planned"
    error: str | None = None
    result_id: int | None = None


class MaintenancePlan(BaseModel):
    operation: str
    applied: bool = False
    summary: dict[str, Any] = {}
    warnings: list[str] = []
    actions: list[PlannedAction] = []
