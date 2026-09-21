# Funmill

Funmill provides one stable task API while execution is delegated to a
replaceable backend. The recommended local backend is self-hosted
[Dagu](https://github.com/dagucloud/dagu), pinned to `v2.16.3`. Windmill remains
available for existing deployments.

```text
client -> Funmill /v1 -> TaskBackend -> Dagu
                                  -> Windmill
```

Funmill owns the public request and response models. Backend job IDs remain
opaque strings, and no backend-specific routes or payloads are exposed to clients.

## Start

Install the project and the Dagu binary. The installer supports macOS and Linux
on Intel/AMD and ARM64:

```bash
uv sync
uv run funmill install dagu
```

Start Dagu in the background. It stores state under
`~/.farfarfun/funmill/services/dagu/data/` and needs no external database:

```bash
uv run funmill start dagu
```

The command reports its PID and log path. Open <http://localhost:8813>, then
start Funmill:

```bash
FUNMILL_API_KEY='replace-me' \
FUNMILL_BACKEND=dagu \
DAGU_URL='http://127.0.0.1:8813' \
uv run funmill start
```

Managed HTTP services bind to `0.0.0.0`: Funmill uses port `8812` and the active
third-party service uses `8813`. Local client URLs still use `127.0.0.1` or
`localhost`; `0.0.0.0` is a listen address, not a client destination.

Dagu starts without authentication. Because it listens on every interface,
restrict port `8813` with a firewall or enable Dagu authentication. When
authentication is enabled, set `DAGU_TOKEN` for both `funmill start dagu` and
`funmill start` so cross-run dependencies can query Dagu from worker processes.

Dagu runs submitted source with the service user's host permissions. Keep both
services private and accept only trusted code; use isolated workers or
containers before accepting untrusted jobs.

The Funmill API port is fixed at `8812`; the active third-party UI/API port is
fixed at `8813`. OpenAPI docs are at <http://localhost:8812/docs>. All `/v1`
routes require `X-API-Key`. See the
[Dagu deployment guide](src/funmill/api/backends/dagu/README.md) or the
[Windmill deployment guide](src/funmill/api/backends/windmill/README.md) for
backend-specific setup.

Run the end-to-end task and DAG checks with:

```bash
FUNMILL_API_KEY=the-value-from-env ./scripts/smoke.sh
```

Set `FUNMILL_CALLBACK_URL` to test callbacks. The URL must be reachable from
the backend workers.

## API

| Operation | Route |
| --- | --- |
| Health check | `GET /health` |
| Submit Python/Bash | `POST /v1/tasks` |
| Submit a DAG | `POST /v1/workflows` |
| Status | `GET /v1/tasks/{task_id}` |
| Logs | `GET /v1/tasks/{task_id}/logs` |
| Progress | `GET /v1/tasks/{task_id}/progress` |
| Result | `GET /v1/tasks/{task_id}/result` |
| Cancel | `POST /v1/tasks/{task_id}/cancel` |
| Rerun | `POST /v1/tasks/{task_id}/rerun` |

`GET /health` is unauthenticated and reports whether the Funmill process and
its configured backend are reachable; it returns `{"status": "ok", "backend":
"..."}` on success and a 503 with a `detail` message when the backend is
unreachable or misconfigured.

Submit one task, optionally waiting for existing task IDs:

```json
{
  "language": "python",
  "source": "def main(value: int):\n    return value * 2\n",
  "args": {"value": 21},
  "depends_on": ["EXISTING_TASK_ID"],
  "dependency_timeout_seconds": 3600,
  "retry": {"attempts": 2, "delay_seconds": 5},
  "timeout_seconds": 300,
  "callback_url": "https://example.internal/task-callback"
}
```

Submit `A -> [B, C]` as one workflow:

```json
{
  "tasks": [
    {"key": "a", "language": "python", "source": "def main(): return 1"},
    {"key": "b", "language": "python", "source": "def main(): return 2", "depends_on": ["a"]},
    {"key": "c", "language": "bash", "source": "main() { echo 3; }", "depends_on": ["a"]}
  ]
}
```

Dependencies inside a workflow are task keys. Top-level `depends_on` values are
IDs returned by earlier Funmill submissions. Backends check those dependencies
from worker jobs, so waiting does not hold the Funmill API process. Failure or
cancellation of a dependency fails the waiting task.
Workflow results and callback payloads are objects keyed by every workflow task
key. Each topological layer is a synchronization barrier; tasks in the same
layer run in parallel.

Callbacks contain `task_id`, `status`, and `payload`; success and failure
delivery retry three times. Delivery is at least once, so callback receivers
must be idempotent.

## Python SDK

A Python client for the `/v1` routes plus `/health` lives in the separate
[`funmill-sdk`](https://github.com/farfarfun/funmill-sdk) repository, published
to PyPI as `funmill` (imported as `funmill.client`):

```bash
pip install funmill
```

```python
from funmill.client import FunmillClient, TaskSubmit

with FunmillClient(base_url="http://127.0.0.1:8812", api_key="replace-me") as client:
    client.health()  # {"status": "ok", "backend": "dagu"}

    accepted = client.submit_task(
        TaskSubmit(language="python", source="def main(): return 21 * 2")
    )
    task = client.get_task(accepted.task_id)
    result = client.get_result(accepted.task_id)
```

See the `funmill-sdk` README for the full client API and error handling.

## Backends

The public contract is `TaskBackend` in `src/funmill/api/backends/base.py`. Backend
selection uses `FUNMILL_BACKEND`; registration lives in
`src/funmill/api/backends/__init__.py`, following the same driver pattern as
`fundrive`. Each third-party adapter lives in its own directory, such as
`src/funmill/api/backends/windmill/`.

Every third-party adapter directory must include a `README.md` covering its
supported platforms, installation, configuration, startup, verification, and
security or operational constraints.

Every managed HTTP service must bind to `SERVICE_BIND_HOST` (`0.0.0.0`). Every
third-party service must use the shared background lifecycle in
`src/funmill/api/backends/service.py` and expose `start`, `stop`, and `status`;
`restart` is composed from `stop` and `start` by the CLI.

Changing the backend does not change `/v1`, but it does not migrate old jobs or
their IDs. Add a Funmill-owned ID mapping database only when jobs must remain
queryable after a live backend migration.

## Operations

```bash
funmill services
funmill install dagu
funmill start dagu
funmill status dagu
funmill restart dagu
funmill stop dagu
funmill install windmill
funmill start windmill
funmill start
```

Third-party services run in the background with PID and log files under
`~/.farfarfun/funmill/services/<service>/`; replace `dagu` with `windmill` in
the lifecycle commands as needed. The Funmill API remains in the foreground and
stops with `Ctrl+C`. Add authentication, TLS, firewall rules, PostgreSQL
backups, callback egress restrictions, and a secrets manager before network
exposure.
