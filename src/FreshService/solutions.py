"""The knowledge base: solution categories, folders and articles.

Articles can only be listed per folder (folder_id is required), and nothing filters by
author. list_my_articles() therefore walks every category -> folder -> sub-folder ->
article and keeps the ones whose `agent_id` (the creator) is you. That is one call per
folder; the API has no separate "owner". "You" is FRESH_AGENT_ID: the API has no
/agents/me, and finding an agent by email needs the "Manage Agents" role.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from datetime import date, datetime
from typing import Any

from FreshService._util import drop_none, workspace
from FreshService.client import Attachments, FreshServiceClient, FreshServiceNotFoundError
from FreshService.models import Article, ArticleStatus, ArticleType, SolutionCategory, SolutionFolder
from FreshService.org import resolve_agent

# -- categories and folders -------------------------------------------------------


def list_categories(client: FreshServiceClient, *, workspace_id: int | None = None) -> list[SolutionCategory]:
    params = drop_none({"workspace_id": workspace(client, workspace_id)})
    return [
        SolutionCategory.model_validate(raw)
        for raw in client.paginate("/solutions/categories", "categories", params)
    ]


def list_folders(
    client: FreshServiceClient,
    category_id: int | None = None,
    *,
    recursive: bool = True,
    workspace_id: int | None = None,
) -> list[SolutionFolder]:
    """Folders of one category (or every category), sub-folders included unless recursive=False."""
    if category_id is None:
        categories = [category.id for category in list_categories(client, workspace_id=workspace_id)]
    else:
        categories = [category_id]
    folders: list[SolutionFolder] = []
    for category in categories:
        top = [
            SolutionFolder.model_validate(raw)
            for raw in client.paginate("/solutions/folders", "folders", {"category_id": category})
        ]
        folders.extend(top)
        if recursive:
            folders.extend(_sub_folders(client, top))
    return folders


def _sub_folders(client: FreshServiceClient, parents: Sequence[SolutionFolder]) -> list[SolutionFolder]:
    found: list[SolutionFolder] = []
    seen = {folder.id for folder in parents}
    queue = list(parents)
    while queue:
        parent = queue.pop(0)
        try:
            children = client.get_list(
                f"/solutions/folders/{parent.id}/sub-folders", ("sub_folders", "folders")
            )
        except FreshServiceNotFoundError:
            continue
        for raw in children:
            folder = SolutionFolder.model_validate(raw)
            if folder.id in seen:
                continue
            seen.add(folder.id)
            if folder.parent_id is None:
                folder.parent_id = parent.id
            if folder.category_id is None:
                folder.category_id = parent.category_id
            found.append(folder)
            queue.append(folder)
    return found


# -- articles ------------------------------------------------------------------------


def list_articles(client: FreshServiceClient, folder_id: int, *, trash: bool = False) -> list[Article]:
    params: dict[str, Any] = {"folder_id": folder_id}
    if trash:
        params["filter"] = "trash"
    return [Article.model_validate(raw) for raw in client.paginate("/solutions/articles", "articles", params)]


def iter_all_articles(
    client: FreshServiceClient,
    *,
    category_ids: Iterable[int] | None = None,
    folders: Iterable[SolutionFolder] | None = None,
) -> Iterator[Article]:
    """Every article in the knowledge base (or the given categories/folders), folder by folder."""
    if folders is None:
        if category_ids is None:
            folders = list_folders(client)
        else:
            folders = [folder for category in category_ids for folder in list_folders(client, category)]
    for folder in folders:
        yield from list_articles(client, folder.id)


def list_my_articles(
    client: FreshServiceClient,
    *,
    agent_id: int | None = None,
    email: str | None = None,
    status: int | None = None,
) -> list[Article]:
    """Articles created by an agent: agent_id, else the agent with `email`, else FRESH_AGENT_ID,
    else the agent with FRESH_AGENT_EMAIL (an email lookup needs the "Manage Agents" role)."""
    if agent_id is None and email is None:
        agent_id = client.settings.agent_id
    if agent_id is None:
        address = email or client.settings.agent_email
        if not address:
            raise ValueError("Pass agent_id or email, or set FRESH_AGENT_ID (or FRESH_AGENT_EMAIL)")
        agent_id = resolve_agent(client, address).id
    return [
        article
        for article in iter_all_articles(client)
        if article.agent_id == agent_id and (status is None or article.status == status)
    ]


def search_articles(
    client: FreshServiceClient, search_term: str, *, user_email: str | None = None
) -> list[Article]:
    """Published articles matching the term that you (or user_email) can see."""
    params = drop_none({"search_term": search_term, "user_email": user_email})
    return [
        Article.model_validate(raw)
        for raw in client.paginate("/solutions/articles/search", "articles", params)
    ]


def get_article(client: FreshServiceClient, article_id: int) -> Article:
    return Article.model_validate(client.get_object(f"/solutions/articles/{article_id}", "article"))


def create_article(
    client: FreshServiceClient,
    *,
    title: str,
    description: str,
    folder_id: int,
    status: int = ArticleStatus.DRAFT,
    article_type: int = ArticleType.PERMANENT,
    tags: Sequence[str] = (),
    keywords: Sequence[str] = (),
    review_date: date | datetime | None = None,
    attachments: Attachments = (),
    **fields: Any,
) -> Article:
    """Create an article (a draft unless status=ArticleStatus.PUBLISHED)."""
    body = drop_none(
        {
            "title": title,
            "description": description,
            "folder_id": folder_id,
            "status": status,
            "article_type": article_type,
            "tags": list(tags) or None,
            "keywords": list(keywords) or None,
            "review_date": review_date.isoformat() if review_date else None,
            **fields,
        }
    )
    return Article.model_validate(
        client.post("/solutions/articles", body, key="article", attachments=attachments)
    )


def update_article(
    client: FreshServiceClient, article_id: int, *, attachments: Attachments = (), **fields: Any
) -> Article:
    """Change title, description, folder_id, status, tags, keywords, review_date, ... and/or
    add attachments."""
    body = drop_none(fields)
    if isinstance(body.get("review_date"), date | datetime):
        body["review_date"] = body["review_date"].isoformat()
    return Article.model_validate(
        client.put(f"/solutions/articles/{article_id}", body, key="article", attachments=attachments)
    )


def publish_article(client: FreshServiceClient, article_id: int) -> Article:
    return update_article(client, article_id, status=ArticleStatus.PUBLISHED)


def send_article_for_approval(client: FreshServiceClient, article_id: int, **fields: Any) -> Article:
    """For folders with approval: submit the article (fields like approver_id per the folder)."""
    return Article.model_validate(
        client.put(f"/solutions/articles/{article_id}/send_for_approval", drop_none(fields), key="article")
    )


def delete_article(client: FreshServiceClient, article_id: int) -> None:
    """Move an article to trash."""
    client.delete(f"/solutions/articles/{article_id}")
