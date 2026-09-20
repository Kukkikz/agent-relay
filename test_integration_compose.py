"""Integration tests against the live Agent Relay + PostgreSQL Compose stack.

Unlike ``test_agent_relay.py`` (which drives the FastAPI app in-process
against a throwaway SQLite database), these tests make real HTTP requests
over the network to a running ``docker compose up`` stack and verify the
protocol works end to end against the actual Postgres-backed service. They
never touch ``RELAY_DATABASE_URL`` and never reset the database - doing so
would wipe live data, unlike the isolated fixture in test_agent_relay.py.

Start the stack first:

    docker compose up -d --build

Then run just these tests:

    uv run pytest -m integration -q

They are excluded from the default `uv run pytest -q` run (see the
`-m "not integration"` addopts in pyproject.toml) and skip automatically if
the stack isn't reachable at BASE_URL.
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

BASE_URL = os.environ.get("AGENT_RELAY_BASE_URL", "http://localhost:8000")

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def client():
    with httpx.Client(base_url=BASE_URL, timeout=10.0) as http_client:
        try:
            response = http_client.get("/health")
        except httpx.ConnectError:
            pytest.skip(f"Agent Relay is not reachable at {BASE_URL}; run `docker compose up -d --build` first.")
        if response.status_code != 200:
            pytest.skip(f"Agent Relay at {BASE_URL} is not healthy: {response.status_code}")
        yield http_client


def register(client: httpx.Client, name: str) -> tuple[dict, dict[str, str]]:
    response = client.post("/api/v1/agents", json={"name": name})
    assert response.status_code == 201, response.text
    data = response.json()
    return data, {"Authorization": f"Bearer {data['token']}"}


def test_stack_is_healthy_and_ready(client: httpx.Client):
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}

    # /ready queries the real Postgres tables, not just connectivity, so a
    # 200 here proves the app and the postgres service are actually wired up.
    ready = client.get("/ready")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}


def test_register_exchange_task_and_result_over_http(client: httpx.Client):
    """SPEC.md acceptance scenario 1, run over the network against the
    live Compose stack: register two agents, exchange a task and its
    result. Names are randomized so repeated runs don't collide."""
    suffix = uuid.uuid4().hex[:8]
    reviewer_name = f"integration-reviewer-{suffix}"
    worker_name = f"integration-worker-{suffix}"
    reviewer, reviewer_headers = register(client, reviewer_name)
    worker, worker_headers = register(client, worker_name)
    print(f"\n[integration] created agent: name={reviewer_name!r} agent_id={reviewer['agent_id']} token={reviewer['token']}")
    print(f"[integration] created agent: name={worker_name!r} agent_id={worker['agent_id']} token={worker['token']}")
    print(f"[integration] paste either token into the dashboard's 'Agent token' box to view this task: {BASE_URL}/")

    sent = client.post(
        "/api/v1/tasks",
        headers=reviewer_headers,
        json={
            "to": worker["agent_id"],
            "input": "Review this Python function: def add(a,b): return a+b",
        },
    )
    assert sent.status_code == 201
    task = sent.json()
    assert task["status"] == "queued"
    task_id = task["task_id"]
    print(f"[integration] created task: task_id={task_id}")

    claim = client.post(
        "/api/v1/tasks/claim",
        headers=worker_headers,
        json={"worker_id": "integration-worker-process", "wait_seconds": 5},
    )
    assert claim.status_code == 200
    claim_data = claim.json()
    assert claim_data["task_id"] == task_id
    assert claim_data["from"] == reviewer["agent_id"]
    assert claim_data["attempt"] == 1

    complete = client.post(
        f"/api/v1/tasks/{task_id}/complete",
        headers=worker_headers,
        json={
            "claim_token": claim_data["claim_token"],
            "output": "The function has no off-by-one issues; looks correct.",
        },
    )
    assert complete.status_code == 200
    assert complete.json() == {"task_id": task_id, "status": "completed"}

    result = client.get(f"/api/v1/tasks/{task_id}", headers=reviewer_headers)
    assert result.status_code == 200
    result_data = result.json()
    assert result_data["status"] == "completed"
    assert result_data["output"] == "The function has no off-by-one issues; looks correct."
    assert result_data["error"] is None
    assert result_data["attempt_count"] == 1
    assert result_data["from"] == reviewer["agent_id"]
    assert result_data["to"] == worker["agent_id"]


def test_unauthenticated_request_is_rejected(client: httpx.Client):
    response = client.get("/api/v1/agents")
    assert response.status_code == 401
