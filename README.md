# FreshServiceLibrary

Python bindings and a CLI for the [Freshservice](https://www.freshworks.com/freshservice/) API v2. Functions return pydantic models, and `model_dump(mode="json")` turns any of them into a plain dict ready for JSON.

- **Service desk:** tickets and service requests (create, update, notes, replies, attachments), the service catalog, changes and change notes, solution articles (create, update, publish, "mine"), announcements and alerts.
- **People:** agents, groups, roles, departments and locations.
- **Assets and software:** assets and asset types, devices, products, vendors and software (each entry carries its publisher's name), the list of software publishers, plus "where is this software installed" by publisher, name and version.
- **Clean-up:** placeholder ("Unknown") product and vendor for assets that have none, setting the real product and vendor later, merging duplicate vendors, merging software within one vendor, and deleting unused software. Each of these is planned first and only written with `--apply`.

## Install

```sh
uv add "freshservice-library @ git+https://github.com/HackITGuys/FreshServiceLibrary@v1.0.0"
```

It installs as `freshservice-library`, imports as `FreshService` and needs Python 3.12 or newer.

> **Upgrading from the pre-1.0 `FreshService.Client`:** that module is gone. `FreshService().add_note(ticket_id=..., message=...)` becomes `add_note(FreshServiceClient(), ticket_id, markdown_to_html(message))`. `markdown_to_html` now lives in `FreshService.formatting` and no longer treats its argument as a file path; use `markdown_file_to_html` for files. Settings are read when a client is created, not at import, and the old `.env` names (`FRESH_KEY`, `FRESH_TEMPLATE_FILEPATH`, `FRESH_DEFAULT_CONTACT_EMAIL`, `FRESH_DEFAULT_DEPT_ID`) still work.

## Credentials

1. In Freshservice, open your **Profile settings** and copy **Your API Key**. The key acts as that agent, with that agent's role. A role without "Manage Agents" can't list agents, for example. Every write is attributed to that agent.
2. Put it in a `.env` file (see [.env.example](.env.example)):

```dotenv
FRESH_DOMAIN=yourcompany.freshservice.com   # not a custom CNAME: the API only answers here
FRESH_API_KEY=...
FRESH_WORKSPACE_ID=2                        # optional; unset = the primary workspace
FRESH_AGENT_ID=123                          # optional; what --mine means (your /agents/<id>)
FRESH_PORTAL_URL=https://support.example.com  # optional; base for links in reports
```

Settings come from environment variables first and then from `.env` in the current directory. `FreshServiceSettings(_env_file="other.env")` or `FreshServiceSettings(domain=..., api_key=...)` overrides both.

## Library

```python
from FreshService import (
    FreshServiceClient,
    markdown_to_html,
    add_note,
    create_change,
    add_change_note,
    create_service_request,
    list_my_articles,
    create_article,
    where_installed,
    summarise_installs,
    plan_tag_unknown,
    apply_plan,
)

with FreshServiceClient() as client:
    add_note(client, 1234, markdown_to_html("**Patched**: see report"), attachments=["report.pdf"])

    change = create_change(
        client,
        subject="Upgrade FW firmware",
        description="<p>...</p>",
        email="me@example.com",
        planned_start_date="2026-12-01T20:00:00Z",
    )
    add_change_note(client, change.id, "<p>Rollback plan attached.</p>", attachments=["rollback.md"])

    create_service_request(
        client,
        item_display_id=12,
        requested_for="user@example.com",
        custom_fields={"cf_laptop_model": "MacBook Air"},
    )

    mine = list_my_articles(client)  # FRESH_AGENT_ID, or agent_id=/email=

    old = where_installed(client, vendor="Mozilla", name="Firefox", version="<128")
    print(summarise_installs(old))  # devices per version

    plan = plan_tag_unknown(client, ["Firewall", "Switch"])  # reads only
    print(plan.model_dump_json(indent=2))
    apply_plan(client, plan)  # writes
```

| Area | Functions |
|---|---|
| Tickets | `get_ticket`, `list_tickets`, `filter_tickets`, `create_ticket`, `create_ticket_from_template`, `update_ticket`, `delete_ticket`, `restore_ticket`, `list_ticket_fields`, `load_templates`, `ticket_from_template` |
| Conversations | `add_note`, `reply`, `list_conversations`, `update_conversation`, `delete_conversation`, `delete_conversation_attachment`, `delete_ticket_attachment` |
| Service requests | `list_catalog_categories`, `list_catalog_items`, `search_catalog_items`, `get_catalog_item`, `create_service_request`, `list_requested_items`, `update_requested_item` (notes: `add_note`) |
| Changes | `get_change`, `list_changes`, `create_change`, `update_change`, `delete_change`, `list_change_fields`, `add_change_note`, `list_change_notes`, `get_change_note`, `update_change_note`, `delete_change_note` |
| Solution articles | `list_categories`, `list_folders`, `list_articles`, `iter_all_articles`, `list_my_articles`, `search_articles`, `get_article`, `create_article`, `update_article`, `publish_article`, `send_article_for_approval`, `delete_article` |
| Announcements | `list_announcements`, `get_announcement`, `create_announcement`, `update_announcement`, `delete_announcement` |
| Alerts | `list_alerts`, `alert_query`, `get_alert`, `list_alert_notes` |
| Org | `list_agents`, `get_agent`, `resolve_agent`, `list_groups`, `list_roles`, `list_departments`, `list_locations` |
| Assets | `list_assets`, `list_devices`, `get_asset`, `update_asset`, `set_asset_product_vendor`, `delete_asset`, `assets_by_display_id`, `list_asset_types`, `asset_type_tree`, `asset_type_fields`, `product_vendor_fields` |
| Products / vendors | `list_products`, `get/create/update/delete_product`, `list_vendors`, `get/create/update/delete_vendor`, `find_or_create_product`, `find_or_create_vendor`, `list_contracts` |
| Software | `list_software`, `list_publishers`, `get/create/update/delete_software`, `list_installations`, `add_installation`, `remove_installations`, `list_software_users`, `add_software_users`, `remove_software_users`, `list_licenses`, `where_installed`, `summarise_installs`, `by_device` |
| Clean-up | `plan_tag_unknown`, `plan_set_product_vendor`, `plan_vendor_merge`, `plan_software_merge`, `plan_software_delete`, `apply_plan` |

Bodies (`description`, `body`, `body_html`) are HTML and sent as given; call `markdown_to_html()` first for Markdown. Attachments are local file paths; with any, the request goes as multipart. For anything not wrapped, `client.get(path)`, `client.paginate(path, key)`, `client.post/put/delete` give you raw JSON with auth, retries and paging handled.

### What the API does and doesn't offer

- **"My articles" means "created by me".** Articles can only be listed per folder, and nothing filters by author, so `list_my_articles` walks every folder (one call each) and matches the creator (`agent_id`). There is no separate owner, and no `/agents/me`: set `FRESH_AGENT_ID`, or `FRESH_AGENT_EMAIL` if your role has "Manage Agents".
- **Change notes can't hold attachments.** On notes, `attachments` is read-only. `add_change_note(attachments=...)` uploads the files to the change and links them in the note.
- **Service requests take files only in the catalog item's own attachment fields.** Free attachments passed to `create_service_request` are added as a private note on the new request.
- **There's no merge endpoint for vendors or software.** Merges are done client-side and aren't atomic. Every action reports done, failed or skipped, and a re-run picks up what's left. Moved installations lose their original `created_at`. Licenses can't be moved, so software with licenses is left alone. Discovery may re-create a merged-away entry on its next scan.
- **Products don't link to vendors.** A product belongs to one asset type and has only a free-text manufacturer. An asset's vendor is its own field, `vendor_<type id>` in `type_fields`, like `product_<type id>`; `product_vendor_fields()` finds the real names.
- **Assets can't be filtered by product or vendor**, and the asset filter stops at 1200 results (the library raises `FreshServiceTruncatedError` rather than return part of them). Type filters read the full asset listing, and product/vendor work also needs `type_fields`, which makes each page cost 3 API credits.
- **There's no fleet-wide installations list in the classic API** (`/itam/software_installations` exists only on accounts created after 31 March 2026). `where_installed` makes one call per matching software entry, in parallel (`FRESH_MAX_WORKERS`), and skips entries that report no installations.
- **Alerts can't be created** through the API, and need the Alert Management feature. Agent roles are read-only. Ticket lists without `--since` cover only tickets created in the last 30 days.
- **Rate limits are per account and per minute** (100 to 500 depending on plan, lower for listing tickets, assets and agents), shared with every other integration. The client honours `Retry-After` and pauses when `X-RateLimit-Remaining` runs low.

## CLI

Every command prints JSON on stdout; logs and errors go to stderr. Bodies are Markdown (`--body`, `--body-file FILE` or `-` for stdin) unless `--html` is given.

```sh
freshservice tickets note 1234 --body-file update.md --attach report.pdf     # private by default
freshservice tickets create --template ADVISORY --body-file advisory.md
freshservice tickets filter "status:2 AND priority:>2"
freshservice requests items --search laptop
freshservice requests create 12 --requested-for user@example.com --field cf_model=MacBook --attach quote.pdf
freshservice changes create --subject "FW upgrade" --body-file plan.md --risk high --type standard --start 2026-12-01T20:00:00
freshservice changes note 55 --body "Rollback plan" --attach rollback.md
freshservice articles list --mine --status draft
freshservice articles create --title "VPN on macOS" --folder 20 --body-file vpn.md --tag vpn
freshservice announcements create --title "Email outage" --body "Back by 14:00" --till 2026-11-13
freshservice alerts list --severity critical --state open --since 2026-11-01
freshservice org groups
freshservice assets devices
freshservice software publishers                         # vendors that publish software, with counts
freshservice software list --publisher Mozilla           # by name (substring/glob) or id; publisher_name filled in
freshservice software list --no-publisher                # entries with no (existing) publisher
freshservice software where --publisher Mozilla --name Firefox --version "<128" --summary
```

Global options: `--env-file`, `-o/--output FILE`, `--compact`, `--audit-log FILE` (every POST/PUT/DELETE as JSON lines) and `-v`. Single deletes ask for confirmation unless `--yes` is given.

### Clean-up: plan, review, `--apply`

These commands print a plan (every write with before/after values) and change nothing until you run them again with `--apply`:

```sh
# 1. Firewalls and switches with no product/vendor get vendor "Unknown" and product "Unknown (<type>)".
freshservice assets tag-unknown --type Firewall --type Switch
freshservice assets tag-unknown --type Firewall --type Switch --apply

# 2. Later, set the real ones on a selection, e.g. every Unknown-vendor firewall named fgt-*.
freshservice assets set-product-vendor --type Firewall --current-vendor Unknown --name "fgt-*" \
    --vendor Fortinet --product "FortiGate 100F" --create-missing --apply

# Merge duplicate vendors: software publisher, asset vendor and contracts move to --into.
freshservice vendors merge "Microsoft Corp*" "MS" --into "Microsoft"            # keeps the sources
freshservice vendors merge "Microsoft Corp*" --into "Microsoft" --delete --apply

# Merge software within one vendor: installations and users move, sources become "ignored".
freshservice software merge --vendor Google --into "Google Chrome" "Google Chrome 1*"
freshservice software merge --vendor Google --into "Google Chrome" "Google Chrome 1*" --delete --apply

# Delete software entries with no installations, users or licenses.
freshservice software delete --status none --apply
```

Placeholder products are per asset type, because a product belongs to exactly one type. They share the `Unknown` prefix (`FRESH_UNKNOWN_LABEL`) so they can be filtered together.

## Development

[devenv](https://devenv.sh) provides Python and [uv](https://docs.astral.sh/uv/), and runs `uv sync` when you enter the shell (direnv does this automatically via `.envrc`):

```sh
devenv shell
uv run pytest                    # unit tests, no network
uv run pytest -m live -rs        # read-only calls to the account in your .env
uv run ruff check . && uv run ruff format --check . && uv run mypy src tests
devenv test                      # all of the above except live
uv build                         # wheel + sdist in dist/
```

Dependencies are managed with `uv add` / `uv remove`. Commit `uv.lock`.

### Releases

Packages are built only from tags. Bump the version, commit, and push a matching `v` tag:

```sh
uv version --bump minor          # or patch / major; updates pyproject.toml and uv.lock
git commit -am "Release $(uv version --short)"
git tag "v$(uv version --short)" && git push origin main --tags
```

The [release workflow](.github/workflows/release.yml) runs the CI checks and fails if the tag doesn't match the version in `pyproject.toml`. It then builds the wheel and sdist and attaches them to a GitHub release with generated notes. Other projects can then pin the tag (`...FreshServiceLibrary@v1.0.0`).

### Dependency updates

[Dependabot](.github/dependabot.yml) checks the uv dependencies and GitHub Actions every Monday at 23:00 (Europe/Oslo). It opens one grouped PR for minor and patch updates and one for major updates. [CI](.github/workflows/ci.yml) runs lint, type checks and tests on Python 3.12 and 3.14 for every PR. Merging stays manual.

- Security-alert PRs are switched on in the repository settings (Code security → Dependabot security updates), not in the config file.
- Dependabot can't update `devenv.lock` (Nix). Run `devenv update` for that.
