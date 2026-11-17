# FreshServiceLibrary

Python bindings and a command-line tool (`freshservice`) for the [Freshservice](https://www.freshworks.com/freshservice/) API v2. Library functions return pydantic models; `model_dump(mode="json")` turns any of them into a plain dict.

**Contents:** [What it does](#what-it-does) · [Running it](#running-it) · [Development and testing](#development-and-testing) · [Releasing a new version](#releasing-a-new-version)

## What it does

| Area | What you can do |
|---|---|
| **Tickets** | Get, list, filter, create (also from templates), update, delete. Add notes and replies with attachments. |
| **Service requests** | Browse the service catalog, request an item, list what was requested. Notes work as on tickets. |
| **Changes** | Get, list, create, update and delete changes, with attachments. Add, edit and delete change notes. |
| **Solution articles** | Categories and folders. List, search, create, update, publish and delete articles. List the articles you created ("mine"). |
| **Announcements** | List, view, create, edit and delete. |
| **Alerts** | List and filter alerts (by severity, state, time), view one, read its notes. |
| **Organisation** | Agents, groups, roles, departments, locations. |
| **Assets and devices** | Assets and asset types; devices (assets under configurable types, default "Hardware"). Set an asset's product and vendor. |
| **Products and vendors** | List, view, create, update, delete. |
| **Software** | Software entries with their publisher's name and usage (devices per version). The publishers and their software. Where a software is installed, by publisher, name and version (exact, glob or range such as `<128`). |
| **Clean-up** | Placeholder "Unknown" product/vendor for assets that have none, and setting the real ones later. Merging duplicate vendors, and software within one vendor. Deleting unused software, then publishers left with none. |

Three things hold across all of it:

- **Bulk changes are planned first.** Clean-up commands print every write they would make (with before/after values) and change nothing until run again with `--apply`. Nothing is deleted without an explicit flag.
- **Quick views.** Every listing takes `--brief`: only the identifying fields (names, state, counts) as a table on screen, with no extra API calls.
- **Results stay data.** The CLI prints JSON (or a table for `--brief`), `-o FILE` always writes JSON, and the library returns models and dicts.

### What the Freshservice API does and doesn't offer

- **"My articles" means "created by me".** Articles can only be listed per folder and nothing filters by author, so the library walks every folder and matches the creator. There is no `/agents/me`: set `FRESH_AGENT_ID`.
- **Change notes can't hold attachments.** Files given for a change note are attached to the change and linked in the note.
- **Service requests take files only in the catalog item's own attachment fields.** Other attachments are added as a private note on the new request.
- **There's no merge endpoint for vendors or software.** Merges are done client-side and aren't atomic; every action reports done, failed or skipped, and a re-run finishes the job. Licenses can't be moved, so software with licenses is left alone.
- **Products don't link to vendors.** A product belongs to one asset type; an asset's vendor and product are separate type fields (`vendor_<type id>`, `product_<type id>`).
- **Assets can't be filtered by product or vendor**, and the asset filter stops at 1200 results (the library raises `FreshServiceTruncatedError` rather than return part of them).
- **There's no publishers endpoint and no fleet-wide installations list.** Publisher counts read every vendor and every software entry; "where installed" makes one call per matching software entry. Pages are fetched in parallel (`FRESH_MAX_WORKERS`, default 8), so a full publisher list takes seconds.
- **Alerts can't be created**, and need the Alert Management feature. Ticket lists without `--since` cover only the last 30 days.
- **Rate limits are per account and per minute** (100 to 500 depending on plan), shared with every other integration. The client honours `Retry-After` and slows down when few calls are left.
- **What a key may do is its agent's role.** A role without "Manage Agents" can't list agents, for example. Where only a name lookup is forbidden, `--brief` shows the id instead.

## Running it

### Install

```sh
uv add "freshservice-library @ git+https://github.com/HackITGuys/FreshServiceLibrary@v1.3.0"   # in a project
uv tool install "git+https://github.com/HackITGuys/FreshServiceLibrary@v1.3.0"            # just the CLI
```

It installs as `freshservice-library`, imports as `FreshService` and needs Python 3.12 or newer. Pin a release tag (see [Releases](https://github.com/HackITGuys/FreshServiceLibrary/releases)).

> **Upgrading from pre-1.0:** `FreshService.Client` is gone. `FreshService().add_note(ticket_id=..., message=...)` becomes `add_note(FreshServiceClient(), ticket_id, markdown_to_html(message))`, and `markdown_to_html` lives in `FreshService.formatting`. The old `.env` names (`FRESH_KEY`, `FRESH_TEMPLATE_FILEPATH`, `FRESH_DEFAULT_CONTACT_EMAIL`, `FRESH_DEFAULT_DEPT_ID`) still work.

### Configure

1. In Freshservice, open **Profile settings** and copy **Your API Key**. The key acts as you, with your role; every write is attributed to you.
2. Put the settings in a `.env` file in the directory you run from (see [.env.example](.env.example)). Environment variables take precedence; `--env-file` (CLI) or `FreshServiceSettings(_env_file=...)` points elsewhere.

| Setting | Default | |
|---|---|---|
| `FRESH_DOMAIN` | required | `yourcompany.freshservice.com`. Not a custom CNAME: the API only answers here. |
| `FRESH_API_KEY` | required | Your API key. |
| `FRESH_WORKSPACE_ID` | primary workspace | Workspace for lists and creates. |
| `FRESH_AGENT_ID` | | You, for "my articles" (the number in your agent profile URL). |
| `FRESH_PORTAL_URL` | `https://{domain}` | Base for links in reports, e.g. your portal's CNAME. |
| `FRESH_DEVICE_ASSET_TYPES` | `Hardware` | Asset types (comma-separated) that count as devices; child types included. |
| `FRESH_UNKNOWN_LABEL` | `Unknown` | Name of the placeholder vendor and product prefix. |
| `FRESH_TEMPLATE_FILE`, `FRESH_DEFAULT_*` | | Ticket templates (see [examples/ticket_templates.json](examples/ticket_templates.json)) and their defaults. |
| `FRESH_MAX_WORKERS` | `8` | Requests in flight for paging and fan-outs. |
| `FRESH_TIMEOUT`, `FRESH_MAX_ATTEMPTS` | `60`, `5` | Per-request timeout (seconds) and attempts. |
| `FRESH_DEBUG`, `FRESH_DEBUG_FILE` | off | Debug output; see [Debugging](#debugging). |

### Command line

`freshservice <area> <action>`, with `--help` at every level. Output is JSON on stdout (tables for `--brief` and `publishers list`); logs, errors and a live progress line go to stderr. Bodies are Markdown (`--body`, `--body-file FILE`, or `-` for stdin) unless `--html` is given.

```sh
# Tickets, requests, changes
freshservice tickets list --brief                                # id, subject, group, state
freshservice tickets note 1234 --body-file update.md --attach report.pdf     # private by default
freshservice tickets create --template ADVISORY --body-file advisory.md
freshservice requests create 12 --requested-for user@example.com --field cf_model=MacBook
freshservice changes create --subject "FW upgrade" --body-file plan.md --risk high --start 2026-12-01T20:00:00
freshservice changes note 55 --body "Rollback plan" --attach rollback.md

# Knowledge base, announcements, alerts, organisation
freshservice articles list --mine --status draft
freshservice articles create --title "VPN on macOS" --folder 20 --body-file vpn.md --tag vpn
freshservice announcements create --title "Email outage" --body "Back by 14:00" --till 2026-11-13
freshservice alerts list --severity critical --state open --since 2026-11-01 --brief
freshservice org groups --brief

# Assets, vendors, software
freshservice assets devices --brief                              # id, name, type, vendor, product, state
freshservice vendors get 11000177691 11001148623 --brief
freshservice publishers list --search micro                      # publishers with software/install counts
freshservice publishers software Mozilla                         # its software, devices per version
freshservice software where --publisher Mozilla --name Firefox --version "<128" --brief
freshservice --json tickets list --brief | jq '.[] | select(.state == "open")'
```

Global options go before the area: `--env-file`, `-o/--output FILE`, `--json`, `--compact`, `--audit-log FILE` (every POST/PUT/DELETE as JSON lines), `--progress/--no-progress`, `--debug`, `--debug-file FILE`, `-v`. Single deletes ask for confirmation unless `--yes` is given.

#### Clean-up: plan, review, `--apply`

```sh
# Assets with no product/vendor get vendor "Unknown" and product "Unknown (<type>)"...
freshservice assets tag-unknown --type Firewall --type Switch --brief
freshservice assets tag-unknown --type Firewall --type Switch --apply
# ...and the real ones are set later on a selection.
freshservice assets set-product-vendor --type Firewall --current-vendor Unknown --name "fgt-*" \
    --vendor Fortinet --product "FortiGate 100F" --create-missing --apply

# Duplicate vendors: software, asset vendor fields and contracts move to --into (--delete removes the sources).
freshservice vendors merge "Microsoft Corporation" --into Microsoft --apply

# Software within one vendor: installations and users move; sources become "ignored" (or --delete).
freshservice software merge --vendor Google --into "Google Chrome" "Google Chrome 1*" --apply

# Unused software (no installations, users or licenses), then publishers left without software.
# A vendor still used by assets, contracts or purchase orders is kept. Safe to schedule.
freshservice software cleanup --brief
freshservice software cleanup --apply
```

### Library

```python
from FreshService import (
    FreshServiceClient,
    add_note,
    apply_plan,
    brief,
    create_change,
    list_publishers,
    list_tickets,
    markdown_to_html,
    plan_software_cleanup,
    where_installed,
)

with FreshServiceClient() as client:  # FRESH_* from the environment / .env
    add_note(client, 1234, markdown_to_html("**Patched**"), attachments=["report.pdf"])
    change = create_change(
        client,
        subject="Upgrade FW firmware",
        description="<p>...</p>",
        email="me@example.com",
        planned_start_date="2026-12-01T20:00:00Z",
    )

    rows = brief(client, list_tickets(client))  # [{"id", "subject", "group", "state"}, ...]
    publishers = list_publishers(client, name="micro")
    old_firefox = where_installed(client, vendor="Mozilla", name="Firefox", version="<128")

    plan = plan_software_cleanup(client)  # reads only
    print(plan.model_dump_json(indent=2))
    apply_plan(client, plan)  # writes
```

Every function takes the client first. Bodies are HTML (use `markdown_to_html()` for Markdown); attachments are file paths. The modules map to the areas above: `tickets`, `service_requests`, `changes`, `solutions`, `announcements`, `alerts`, `org`, `assets`, `catalog` (products, vendors), `software`, `maintenance` (plans), `views` (`brief`, `render_table`). For anything not wrapped, `client.get(path)`, `client.paginate(path, key)` and `client.post/put/delete` handle auth, retries and paging. Pass `progress=callback` to `FreshServiceClient` to follow long listings.

### Debugging

With debugging on, every request is logged: method, path, status, time, attempt, calls left this minute and Freshservice's request id. So are request bodies, retries and the full response of every failed request. Errors carry the request id too (`exc.request_id`, and `[request id ...]` in the message); quote it to Freshservice support. The API key is never logged, and bodies are cut at 2,000 characters.

```sh
freshservice --debug tickets get 1234                       # on stderr, with a traceback on failure
freshservice --debug-file fs-debug.log software cleanup --apply   # into a file only
```

From another program, the library logs to the standard `logging` loggers `FreshService` (retries, fallbacks, maintenance actions) and `FreshService.http` (each request):

```python
import logging

from FreshService import FreshServiceClient, enable_debug

logging.getLogger("FreshService").setLevel(logging.DEBUG)  # if the app already configures logging
enable_debug(file="fs-debug.log")  # if it doesn't: stderr and/or a file
client = FreshServiceClient(debug=True)  # the same; so do FRESH_DEBUG / FRESH_DEBUG_FILE
```

Connection errors and timeouts on GET, PUT and DELETE are retried with backoff; a POST is never retried, because it may already have gone through.

## Development and testing

[devenv](https://devenv.sh) provides Python 3.14 and [uv](https://docs.astral.sh/uv/), installs the git hooks (ruff), and runs `uv sync` when you enter the shell; with direnv this happens on `cd` (via `.envrc`).

```sh
devenv shell                      # or let direnv load it
uv run freshservice --help        # the CLI from your working copy (installed in editable mode)
```

| Command | What it checks |
|---|---|
| `uv run pytest` | Unit tests against a mocked API (`responses`); no network, no `.env` needed. |
| `uv run pytest -m live -rs` | Read-only calls to the account in your `.env`; endpoints your role may not use are reported as skipped. Never writes. |
| `uv run ruff check . && uv run ruff format --check .` | Lint and formatting (also the Python blocks in this README). |
| `uv run mypy src tests` | Strict type checks. |
| `devenv test` | All of the above except live, as CI runs them. |

- **Tests** live in `tests/`, one file per area; `tests/conftest.py` has the mocked client and helpers (`pages()` serves a paged endpoint the way Freshservice does). The mock refuses any unregistered request, so a test that registers only GETs also proves nothing was written.
- **Writes against the real API** are not in the automatic tests. Try them on a test ticket, change or article, and run clean-up commands without `--apply` first.
- **Dependencies:** `uv add` / `uv remove`, and commit `uv.lock`. [Dependabot](.github/dependabot.yml) opens grouped PRs against `develop` every Monday (minor/patch together, major separately); it can't update `devenv.lock`, so run `devenv update` for that.
- **CI** ([ci.yml](.github/workflows/ci.yml)) runs lint, types and tests on Python 3.12 and 3.14 for every PR and for pushes to `main`.

## Releasing a new version

Work happens on `develop`; `main` only moves through pull requests. A release is a version bump and a `v` tag on `develop`; the tag builds the release, and a PR brings `main` up to date.

```sh
git switch develop && git pull                           # 1. start from the latest develop
devenv test                                              # 2. everything green
uv version --bump minor                                  # 3. or patch / major (updates pyproject.toml and uv.lock)
git commit -am "Release $(uv version --short)"
git tag "v$(uv version --short)"
git push origin develop "v$(uv version --short)"         # 4. push the branch and that one tag
```

5. Pushing the tag starts the [release workflow](.github/workflows/release.yml): it runs the CI checks, fails if the tag doesn't match `version` in `pyproject.toml`, builds the wheel and sdist, smoke-tests `freshservice --version`, and publishes a GitHub release with generated notes.
6. Open a pull request `develop` → `main` and merge it.
7. Bring your local `main` along without switching to it: `git fetch origin main:main`.

Then point consumers at the new tag (`...FreshServiceLibrary@vX.Y.Z`) and run `uv lock --upgrade-package freshservice-library` there.

- **Patch** for fixes, **minor** for new commands or options, **major** when existing commands, output or function signatures change.
- Never push your local `main`, and don't force-push: if git rejects a push to `main`, your copy is behind, and the PR is the way to update it. `git push --tags` pushes every local tag; pushing the one tag by name, as above, avoids surprises.
- A wrong tag that hasn't been released yet can be removed with `git push origin :refs/tags/vX.Y.Z` and `git tag -d vX.Y.Z`. Once a release exists, publish a new patch version instead.
