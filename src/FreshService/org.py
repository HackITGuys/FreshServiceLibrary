"""People and organisation: agents, agent groups, roles, departments, locations."""

from __future__ import annotations

from FreshService._util import drop_none, workspace
from FreshService.client import FreshServiceClient, FreshServiceNotFoundError
from FreshService.models import Agent, Department, Group, Location, Role


def list_agents(
    client: FreshServiceClient,
    *,
    email: str | None = None,
    active: bool | None = None,
    state: str | None = None,
) -> list[Agent]:
    """Agents, optionally by email, active (True/False) or state (fulltime/occasional)."""
    params = drop_none(
        {
            "email": email,
            "active": None if active is None else str(active).lower(),
            "state": state,
        }
    )
    return [Agent.model_validate(raw) for raw in client.paginate("/agents", "agents", params)]


def get_agent(client: FreshServiceClient, agent_id: int) -> Agent:
    return Agent.model_validate(client.get_object(f"/agents/{agent_id}", "agent"))


def resolve_agent(client: FreshServiceClient, email: str) -> Agent:
    """The agent with this email address (the API has no /agents/me)."""

    def lookup() -> Agent:
        wanted = email.strip().casefold()
        for agent in list_agents(client, email=email.strip()):
            if (agent.email or "").casefold() == wanted:
                return agent
        raise FreshServiceNotFoundError(f"No agent has the email address {email!r}")

    return client.memo(f"agent:{email.strip().casefold()}", lookup)


def list_groups(client: FreshServiceClient, *, workspace_id: int | None = None) -> list[Group]:
    """Agent groups (of one workspace; the primary one by default)."""
    params = drop_none({"workspace_id": workspace(client, workspace_id)})
    return [Group.model_validate(raw) for raw in client.paginate("/groups", "groups", params)]


def list_roles(client: FreshServiceClient) -> list[Role]:
    return [Role.model_validate(raw) for raw in client.paginate("/roles", "roles")]


def list_departments(client: FreshServiceClient) -> list[Department]:
    return [Department.model_validate(raw) for raw in client.paginate("/departments", "departments")]


def list_locations(client: FreshServiceClient) -> list[Location]:
    return [Location.model_validate(raw) for raw in client.paginate("/locations", "locations")]
