# Agent Relay (SQLite starter)

Agent Relay is a small FastAPI service for registering agents, delivering one
task at a time, and recording results. The local starter is self-contained:
SQLite persists the queue and attempts, while workers execute tasks on their own
machines. The included worker deterministically returns `input.upper()`.

## Run it

```bash
uv sync
uv run uvicorn main:app --reload
```

Open <http://127.0.0.1:8000/> for the token-based local dashboard. The default
database is `./agent-relay.db`; set `RELAY_DATABASE_URL` to use another SQLite
file. `GET /health` is a liveness check and `GET /ready` verifies database
connectivity and schema (it queries the real tables, so a wiped volume
reports not-ready instead of passing with zero tables).

Register two identities and send a task:

```bash
alice=$(curl -sS -X POST http://127.0.0.1:8000/api/v1/agents \
  -H 'content-type: application/json' -d '{"name":"alice"}')
bob=$(curl -sS -X POST http://127.0.0.1:8000/api/v1/agents \
  -H 'content-type: application/json' -d '{"name":"uppercase"}')
```

The response contains each agent's secret `token` once. Keep it outside source
control. Use `Authorization: Bearer <token>` for all subsequent API calls;
registration is the only unauthenticated endpoint. For a shared installation,
set `RELAY_ENROLLMENT_SECRET` and send it as `X-Enrollment-Secret` when
registering.

## Run the deterministic worker

The worker can register itself and save credentials in a mode-0600 JSON file:

```bash
uv run python main.py worker \
  --base-url http://127.0.0.1:8000 \
  --name uppercase \
  --credentials ./uppercase-credentials.json \
  --worker-id laptop-1
```

For failure/redelivery demonstrations, make local execution intentionally slow
and stop the process after one completion:

```bash
uv run python main.py worker --credentials ./uppercase-credentials.json \
  --slow-seconds 75 --worker-id slow-laptop
```

The worker heartbeats during long work. Killing it leaves the claim leased;
after the 60-second lease expires, another worker can claim the task with a new
token and incremented attempt number. `RELAY_LEASE_SECONDS` and
`RELAY_MAX_ATTEMPTS` are configurable server settings.

An existing credential can also be supplied explicitly (the token is not
written to disk):

```bash
uv run python main.py worker --agent-id agent_123 --token agt_… --worker-id laptop-2
```

## Storage and delivery behavior

`database.py` contains SQLAlchemy models, SQLite WAL setup, and the isolated
`BEGIN IMMEDIATE` transaction helper. `storage.py` contains task/claim/recovery
operations; routes and request models are kept in `main.py` and `schemas.py`.
SQLite does not provide PostgreSQL's `FOR UPDATE SKIP LOCKED`, so the starter
serializes writer transactions to make concurrent claims safe across processes.
Students can port this storage seam to PostgreSQL later without changing the
HTTP protocol or lifecycle in `SPEC.md`.

Claims are at-least-once and leased for 60 seconds by default. Heartbeats extend
an active lease. A completion or failure must include the recipient's bearer
token and claim token. Repeating the exact terminal request with that claim
token is idempotent; a stale token or different result receives `409`.

## Verify

The test suite covers the main protocol, sender/recipient access boundaries,
hashed claim-token behavior, idempotent terminal retries, concurrent claims,
lease expiry before and after recovery, pagination/error shape, and dashboard
asset serving:

```bash
uv run pytest -q
```

Tests default to a scratch database in the platform temp directory (e.g.
`/tmp/agent-relay-test.db` on Linux/macOS, `%TEMP%\agent-relay-test.db` on
Windows) so they don't reset your dev server's `./agent-relay.db`. The
fixture drops and recreates all tables on whatever `RELAY_DATABASE_URL`
points at, so stop the dev server first or set `RELAY_DATABASE_URL` to a
scratch file before running tests against another database.

This starter intentionally does not include CI, external brokers, or an
LLM. Those remain deployment and student-port concerns rather than part of
the local relay protocol.

Also see `test_integration_compose.py`, which exercises a *running*
Docker Compose or Kubernetes deployment over real HTTP (see below) rather
than the in-process `TestClient` used above:

```bash
uv run pytest -m integration -v -s
```

It's excluded from the default `pytest -q` run and skips automatically if
nothing is listening at `http://localhost:8000` (override with
`AGENT_RELAY_BASE_URL`). Run it with `-s` to see the agent names, IDs,
tokens, and task ID it created, e.g. to look them up in the dashboard.

## Run with Docker Compose (PostgreSQL)

```bash
docker compose up -d --build
```

This builds `agent-relay:local` and starts it alongside a `postgres`
service (named `postgres` in `compose.yaml`), wiring `RELAY_DATABASE_URL`
to `postgresql+psycopg://agent_relay:agent_relay@postgres:5432/agent_relay`.
The app waits for Postgres's healthcheck before starting. Postgres data
persists in the `postgres-data` named volume across restarts.

`database.py` picks the writer-transaction strategy by URL scheme: SQLite
uses `BEGIN IMMEDIATE`; PostgreSQL takes a session-scoped advisory lock
(`pg_advisory_xact_lock`) as the first statement of the same transaction
seam, so claim, heartbeat, terminal submission, and recovery serialize
identically on either backend. Running `main.py`/`pytest` directly (outside
Compose) still defaults to the local SQLite file unless `RELAY_DATABASE_URL`
is set.

To build and run the image by hand instead:

```bash
docker build -t agent-relay:local .
docker run -d --name agent-relay -p 8000:8000 \
  -e RELAY_DATABASE_URL=postgresql+psycopg://agent_relay:agent_relay@<postgres-host>:5432/agent_relay \
  agent-relay:local
```

## Run on Kubernetes (kind)

Manifests live in `k8s/`: a `postgres` Deployment backed by a 1Gi
`PersistentVolumeClaim` (data survives pod restarts), a `postgres` Service
so `RELAY_DATABASE_URL` can address it by name, an `agent-relay` Deployment
and Service, and readiness/liveness probes on both (`pg_isready` for
Postgres; `GET /ready` and `GET /health` for the app).

Requires [`kind`](https://kind.sigs.k8s.io/) and `kubectl`. Create a local
cluster, build and load the image, then apply the manifests:

```bash
kind create cluster --name agent-relay
docker build -t agent-relay:local .
kind load docker-image agent-relay:local --name agent-relay
kubectl apply -f k8s/
```

`agent-relay-deployment.yaml` sets `imagePullPolicy: Never`, since the
image only exists in kind's local containerd (loaded above) and was never
pushed to a registry. The app pod calls `init_db()` on startup and will
crash-loop briefly if it starts before Postgres is ready; Kubernetes
restarts it automatically and it recovers once Postgres's readiness probe
passes — no action needed.

Check that both pods are ready:

```bash
kubectl rollout status deployment/postgres
kubectl rollout status deployment/agent-relay
kubectl get pods
```

Open the dashboard through port forwarding:

```bash
kubectl port-forward svc/agent-relay 8000:8000
```

Then visit <http://127.0.0.1:8000/>. Tear down with
`kind delete cluster --name agent-relay`.
