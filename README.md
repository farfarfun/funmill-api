# Funmill

Funmill provides one stable task API while execution is delegated to a
replaceable backend. The recommended local backend is self-hosted
[Dagu](https://github.com/dagucloud/dagu), pinned to `2.17.2` by default and
installed via pnpm from the platform-specific `@dagucloud/dagu-*` npm package
(more reliable than GitHub Releases in regions where GitHub's CDN is slow or
blocked); `funmill install dagu --force` instead installs the latest version
published for the current platform. Windmill remains available for existing
deployments.

```text
client -> Funmill /v1 -> TaskBackend -> Dagu
                                  -> Windmill
```

Funmill owns the public request and response models. Backend job IDs remain
opaque strings, and no backend-specific routes or payloads are exposed to
clients — except for the optional `ui_url` convenience field described below,
which deliberately leaks a backend-specific web UI link as an unverified,
best-effort reference for humans.

## Configuration

Every Funmill setting resolves as **config file > environment variable >
default**. The config file is a dotenv-style file (`#` comments, optional
`export ` prefix, optional quotes) read by every `funmill` subcommand, so
`funmill start dagu` and `funmill start` pick it up without any inline
environment variables:

```dotenv
# ~/.farfarfun/funmill/api/api.env
FUNMILL_API_KEY=replace-me
FUNMILL_BACKEND=dagu
DAGU_URL=http://127.0.0.1:8813
DAGU_AUTH_BASIC_USERNAME=funmill
DAGU_AUTH_BASIC_PASSWORD=funmill
```

The default path is `~/.farfarfun/funmill/api/api.env`; override it with
`funmill --config <path> <command>` or `FUNMILL_CONFIG`. A missing default file
is fine (everything falls back to environment variables), but an explicit
`--config` path that does not exist is an error. `FUNMILL_HOME` is the one
exception and must stay an environment variable, since it determines where the
config file itself lives.

## Start

Install the project and the Dagu binary. The installer supports macOS and Linux
on Intel/AMD and ARM64, and installs Dagu via pnpm, so Node.js and
[pnpm](https://pnpm.io/installation) must be on `PATH` first. If the npm
registry itself is slow to reach, configure a mirror, e.g.
`pnpm config set registry https://registry.npmmirror.com`:

```bash
uv sync
uv run funmill install dagu
```

Start Dagu in the background. It stores state under
`~/.farfarfun/funmill/services/dagu/data/` and needs no external database:

```bash
uv run funmill start dagu
```

The command reports its PID and log path. Open <http://localhost:8813> — Dagu's
Web UI and REST API prompt for HTTP Basic Auth, `funmill`/`funmill` by default
(see below) — then start Funmill:

```bash
FUNMILL_API_KEY='replace-me' \
FUNMILL_BACKEND=dagu \
DAGU_URL='http://127.0.0.1:8813' \
uv run funmill start
```

Like Dagu and Windmill, `funmill start` runs the API in the background and
reports its PID and log path immediately. Use `uv run funmill run` instead to
keep it in the foreground (stop with Ctrl+C), which is convenient for local
debugging.

Managed HTTP services bind to `0.0.0.0`: Funmill uses port `8812` and the active
third-party service uses `8813`. Local client URLs still use `127.0.0.1` or
`localhost`; `0.0.0.0` is a listen address, not a client destination.

`funmill start dagu` enables Dagu's built-in HTTP Basic Auth by default
(`DAGU_AUTH_MODE=basic`), with the account `funmill`/`funmill`
(`DAGU_AUTH_BASIC_USERNAME`/`DAGU_AUTH_BASIC_PASSWORD`) covering both the
browser-facing Web UI and the REST API. Because port `8813` listens on every
interface, still restrict it with a firewall, and set your own
`DAGU_AUTH_BASIC_USERNAME`/`DAGU_AUTH_BASIC_PASSWORD` before starting Dagu on
any shared or network-exposed host instead of keeping the default credentials.

To serve Dagu under a reverse-proxy prefix, set `DAGU_BASE_PATH` (e.g.
`/api/dagu`) once. `funmill start dagu` passes it to Dagu, which moves both the
Web UI and the REST API under it, and `DaguBackend.from_env()` folds the same
prefix into `DAGU_URL`, so there is no second, prefixed URL to keep in sync. If
`DAGU_URL` already ends with the prefix (for instance when it points at the
proxy rather than at Dagu directly), it is left alone.
`funmill start` reads the same two variables (`DaguBackend.from_env()`) to
authenticate its own calls to Dagu, and Dagu forwards them to the dependency-wait
script it runs inside worker processes — so set them identically for both
`funmill start dagu` and `funmill start`. Set `DAGU_AUTH_MODE=none` explicitly
to go back to no authentication (e.g. for fully isolated local development).

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
  "env": {"DJANGO_SETTINGS_MODULE": "myapp.settings"},
  "task_id": "caller-generated-id",
  "depends_on": ["EXISTING_TASK_ID"],
  "dependency_timeout_seconds": 3600,
  "retry": {"attempts": 2, "delay_seconds": 5},
  "timeout_seconds": 300,
  "callback_url": "https://example.internal/task-callback",
  "name": "double-the-value",
  "description": "Doubles the input for the nightly report"
}
```

### Task environment

A backend does not hand its own environment to task subprocesses. Dagu passes
only a whitelist — `DAGU_*`, its own `DAG_*` run metadata, and `HOME`, `LANG`,
`PATH`, `PWD`, `SHELL`, `TERM`, `USER` — so anything else a task needs must be
declared explicitly, in either of two places:

- `env` on the task or workflow task, for per-request values.
- `FUNMILL_TASK_ENV_<NAME>=value` in the Funmill API process environment
  (typically the config file), for host-wide defaults applied to every task. The
  prefix is stripped, so `FUNMILL_TASK_ENV_DJANGO_SETTINGS_MODULE=myapp.settings`
  reaches tasks as `DJANGO_SETTINGS_MODULE`. These are read once at startup;
  restart `funmill` after changing them.

Per-request `env` wins over a host default with the same name. Keys must be
valid shell identifiers (`^[A-Za-z_][A-Za-z0-9_]*$`), and plain (unprefixed)
variables in the API process are never forwarded. The Python interpreter a task
runs under is whichever `python3` comes first on the inherited `PATH`, which is
the `PATH` the backend service itself was started with — so start the backend
from the environment whose interpreter has your packages installed. The working
directory is a fresh per-run scratch directory owned by the backend.

### Caller-supplied task IDs

`task_id` on `POST /v1/tasks` and `POST /v1/workflows` lets a caller generate
the ID up front (write it to their own database, return it to a client, then
submit). It must match `^[A-Za-z0-9_-]+$`, be at most 200 characters, and cannot
be the literal `latest`, which Dagu reserves. IDs are unique per backend: a
duplicate is rejected with **409 Conflict** rather than starting a second run, so
a caller-chosen ID doubles as an idempotency key — retrying a submission that may
already have landed is safe, and a 409 means "already accepted", not "try again".
The Windmill backend rejects `task_id` and `env` with a
400, since Windmill always mints its own job UUID and has no per-step
environment.

Submit `A -> [B, C]` as one workflow:

```json
{
  "tasks": [
    {"key": "a", "language": "python", "source": "def main(): return 1", "name": "seed"},
    {"key": "b", "language": "python", "source": "def main(): return 2", "depends_on": ["a"]},
    {"key": "c", "language": "bash", "source": "main() { echo 3; }", "depends_on": ["a"]}
  ]
}
```

`name`/`description` are optional, purely for telling tasks apart (in batch
submissions too), and are not persisted by Funmill itself — Funmill has no
database. They are stored only if the configured backend itself stores them,
and `GET /v1/tasks/{task_id}` echoes them back only when the backend actually
returned them; otherwise both fields are `null`. Dagu round-trips both for
single-task submissions (they land on the DAG step and are visible in Dagu's
own UI too); for workflow submissions each task's `name`/`description` still
reaches its own step, but `GET /v1/tasks/{task_id}` reports the workflow run
as a whole and therefore leaves `name`/`description` as `null`. Windmill shows
`name` as the job/branch summary in its UI but does not round-trip either
field back through the API.

For single-task submissions, `name` also becomes the Dagu run's own top-level
name (what Dagu's own UI/API lists the run under), instead of every run
sharing the literal name `funmill`. Funmill resolves the actual name lazily
per request — it tries the shared default first and only falls back to
asking Dagu for the real name (by run ID) on a mismatch — so this adds no
extra request for unnamed tasks or workflows. A `name` containing `/` is
rejected defensively (falls back to the shared default) since encoded
slashes inside a single path segment are not reliably handled across HTTP
frameworks; every other character, including spaces and non-ASCII text, is
supported.

`POST /v1/tasks`, `POST /v1/workflows`, `POST /v1/tasks/{task_id}/rerun`, and
`GET /v1/tasks/{task_id}` also return a `ui_url` field: a best-effort deep link
into the configured backend's own web UI for that run (for Dagu,
`{DAGU_URL}/dag-runs/{name}/{task_id}`; for Windmill,
`{WINDMILL_URL}/run/{task_id}?workspace={WINDMILL_WORKSPACE}`). This is a
supplementary reference link only, meant for humans who need to inspect
something beyond what `/v1` itself returns (live graph, raw logs, and so on).
Funmill does not verify it is reachable and does not apply its own
authentication to it — the backend UI may be unauthenticated, may require
separate credentials, or may not be reachable from wherever the link is
opened. `ui_url` is `null` only if the backend cannot compute it; a non-null
value is not a guarantee that opening it will succeed.

Dependencies inside a workflow are task keys. Top-level `depends_on` values are
IDs returned by earlier Funmill submissions. Backends check those dependencies
from worker jobs, so waiting does not hold the Funmill API process. Failure or
cancellation of a dependency fails the waiting task.
Workflow results and callback payloads are objects keyed by every workflow task
key. Each topological layer is a synchronization barrier; tasks in the same
layer run in parallel.

Callbacks contain `task_id`, `status`, and `payload`; success and failure
delivery retry three times. Delivery is at least once, so callback receivers
must be idempotent. `status` is one of `succeeded`, `failed`, or `canceled`.
`payload` is the task's decoded return value on success, and a short
explanatory string such as `Dagu run failed` otherwise:

```json
{"task_id": "034XoTIOluThNya2QztEoo", "status": "succeeded", "payload": {"x": 42}}
```

`retry` re-runs a failed task `attempts` more times, `delay_seconds` apart.
While retrying, `GET /v1/tasks/{task_id}` stays `running` — there is no distinct
"retrying" status — and `GET /v1/tasks/{task_id}/logs` accumulates the output of
every attempt, not just the last one.

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
funmill status
funmill restart
funmill stop
funmill run
```

Every managed service, including Funmill's own API, runs in the background
with PID and log files under `~/.farfarfun/funmill/services/<service>/`;
`start`/`status`/`restart`/`stop` default to the `api` service when no service
name is given, and `dagu`/`windmill` work the same way by passing that name
explicitly. `funmill run` starts the API in the foreground instead (stop with
`Ctrl+C`), for local debugging. Add authentication, TLS, firewall rules,
PostgreSQL backups, callback egress restrictions, and a secrets manager before
network exposure.
