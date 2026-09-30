import base64
import hashlib
import io
import json
import sys
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import funmill.api.cli as funmill_cli
from funmill.api import app, backend_dependency
from funmill.api import service as api_service
from funmill.api.backends import service as backend_service
from funmill.api.backends.base import BackendError, SubmitResult, TaskBackend
from funmill.api.backends.dagu import DaguBackend
from funmill.api.backends.dagu import service as dagu_service
from funmill.api.backends.windmill import WindmillBackend
from funmill.api.backends.windmill import service as windmill_service
from funmill.api.models import (
    TaskInfo,
    TaskLogs,
    TaskProgress,
    TaskResult,
    TaskStatus,
    TaskSubmit,
    WorkflowSubmit,
)

JOB_ID = "11111111-1111-4111-8111-111111111111"
RERUN_ID = "22222222-2222-4222-8222-222222222222"


def workflow(**overrides):
    value = {
        "tasks": [
            {"key": "a", "language": "python", "source": "def main(): return 1"},
            {
                "key": "b",
                "language": "python",
                "source": "def main(): return 2",
                "depends_on": ["a"],
            },
            {
                "key": "c",
                "language": "bash",
                "source": "main() { echo 3; }",
                "depends_on": ["a"],
            },
        ]
    }
    value.update(overrides)
    return WorkflowSubmit.model_validate(value)


def test_workflow_dependency_validation_and_layers():
    layers = [[task.key for task in layer] for layer in workflow().topological_layers()]
    assert layers == [
        ["a"],
        ["b", "c"],
    ]

    with pytest.raises(ValidationError, match="unknown dependencies"):
        workflow(
            tasks=[
                {
                    "key": "a",
                    "language": "python",
                    "source": "def main(): pass",
                    "depends_on": ["missing"],
                }
            ]
        )

    with pytest.raises(ValidationError, match="dependency cycle"):
        workflow(
            tasks=[
                {
                    "key": "a",
                    "language": "python",
                    "source": "def main(): pass",
                    "depends_on": ["b"],
                },
                {
                    "key": "b",
                    "language": "python",
                    "source": "def main(): pass",
                    "depends_on": ["a"],
                },
            ]
        )


def test_windmill_translates_task_dependencies_retry_and_callback():
    captured = {}

    def handler(request: httpx.Request):
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, text=JOB_ID)

    client = httpx.Client(
        base_url="http://windmill/api/w/admins/",
        transport=httpx.MockTransport(handler),
    )
    backend = WindmillBackend("http://unused", "admins", "token", client=client)
    result = backend.submit_task(
        TaskSubmit.model_validate(
            {
                "language": "python",
                "source": "def main(value): return value",
                "args": {"value": 7},
                "depends_on": [RERUN_ID],
                "retry": {"attempts": 2, "delay_seconds": 3},
                "callback_url": "https://example.test/callback",
            }
        )
    )

    assert result.task_id == JOB_ID
    assert result.ui_url == f"http://unused/run/{JOB_ID}?workspace=admins"
    assert captured["path"].endswith("/jobs/run/preview_flow")
    value = captured["body"]["value"]
    assert value["modules"][0]["value"]["type"] == "whileloopflow"
    assert value["modules"][1]["retry"]["constant"] == {
        "attempts": 2,
        "seconds": 3,
    }
    assert value["modules"][-1]["id"] == "funmill_callback"
    assert value["failure_module"]["id"] == "failure"


def test_windmill_translates_dag_to_parallel_layer():
    payloads = []

    def handler(request: httpx.Request):
        payloads.append(json.loads(request.content))
        return httpx.Response(201, text=JOB_ID)

    client = httpx.Client(
        base_url="http://windmill/api/w/admins/",
        transport=httpx.MockTransport(handler),
    )
    backend = WindmillBackend("http://unused", "admins", "token", client=client)
    backend.submit_workflow(workflow())

    modules = payloads[0]["value"]["modules"]
    assert modules[0]["id"] == "a"
    assert modules[1]["value"]["type"] == "branchall"
    assert [branch["summary"] for branch in modules[1]["value"]["branches"]] == [
        "b",
        "c",
    ]
    assert modules[2]["id"] == "funmill_result"
    assert modules[2]["value"]["input_transforms"]["results"]["expr"] == (
        '({"a": results["a"], "b": results["funmill_layer_1"][0], '
        '"c": results["funmill_layer_1"][1]})'
    )


def test_windmill_uses_task_name_as_module_summary():
    payloads = []

    def handler(request: httpx.Request):
        payloads.append(json.loads(request.content))
        return httpx.Response(201, text=JOB_ID)

    client = httpx.Client(
        base_url="http://windmill/api/w/admins/",
        transport=httpx.MockTransport(handler),
    )
    backend = WindmillBackend("http://unused", "admins", "token", client=client)
    backend.submit_task(
        TaskSubmit.model_validate(
            {
                "language": "python",
                "source": "def main(): pass",
                "name": "Import customers",
            }
        )
    )

    module = payloads[0]["value"]["modules"][0]
    assert module["summary"] == "Import customers"


def test_windmill_branch_summary_prefers_name_over_key():
    payloads = []

    def handler(request: httpx.Request):
        payloads.append(json.loads(request.content))
        return httpx.Response(201, text=JOB_ID)

    client = httpx.Client(
        base_url="http://windmill/api/w/admins/",
        transport=httpx.MockTransport(handler),
    )
    backend = WindmillBackend("http://unused", "admins", "token", client=client)
    backend.submit_workflow(
        workflow(
            tasks=[
                {"key": "a", "language": "python", "source": "def main(): return 1"},
                {
                    "key": "b",
                    "language": "python",
                    "source": "def main(): return 2",
                    "depends_on": ["a"],
                    "name": "Load B",
                },
                {
                    "key": "c",
                    "language": "bash",
                    "source": "main() { echo 3; }",
                    "depends_on": ["a"],
                },
            ]
        )
    )

    branches = payloads[0]["value"]["modules"][1]["value"]["branches"]
    assert [branch["summary"] for branch in branches] == ["Load B", "c"]


def test_windmill_reruns_preview_flow_and_normalizes_status():
    requests = []

    def handler(request: httpx.Request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": JOB_ID,
                    "success": True,
                    "canceled": False,
                    "created_at": "2026-09-10T00:00:00Z",
                    "started_at": "2026-09-10T00:00:01Z",
                    "completed_at": "2026-09-10T00:00:02Z",
                    "duration_ms": 1000,
                    "raw_flow": {"modules": []},
                    "args": {},
                },
            )
        return httpx.Response(201, text=RERUN_ID)

    client = httpx.Client(
        base_url="http://windmill/api/w/admins/",
        transport=httpx.MockTransport(handler),
    )
    backend = WindmillBackend("http://unused", "admins", "token", client=client)

    info = backend.get_task(JOB_ID)
    assert info.status == TaskStatus.SUCCEEDED
    assert info.ui_url == f"http://unused/run/{JOB_ID}?workspace=admins"
    assert backend.get_progress(JOB_ID).progress == 100
    rerun_result = backend.rerun(JOB_ID)
    assert rerun_result.task_id == RERUN_ID
    assert rerun_result.ui_url == f"http://unused/run/{RERUN_ID}?workspace=admins"
    assert json.loads(requests[-1].content) == {"value": {"modules": []}, "args": {}}


def test_windmill_service_install_and_start(monkeypatch, tmp_path):
    binary = b"windmill-test-binary"
    monkeypatch.delenv("FUNMILL_HOME", raising=False)
    assert windmill_service._target() == (
        Path.home() / ".farfarfun" / "funmill" / "services" / "windmill" / "windmill"
    )
    assert windmill_service._config_path() == (
        Path.home() / ".farfarfun" / "funmill" / "services" / "windmill" / ".env"
    )
    monkeypatch.setenv("FUNMILL_HOME", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("PORT", "9999")
    monkeypatch.setattr(windmill_service.platform, "system", lambda: "Linux")
    monkeypatch.setattr(windmill_service.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(windmill_service, "SHA256", hashlib.sha256(binary).hexdigest())
    monkeypatch.setattr(
        windmill_service,
        "urlopen",
        lambda *_args, **_kwargs: io.BytesIO(binary),
    )

    executable = windmill_service.install()
    assert executable.read_bytes() == binary
    assert executable.stat().st_mode & 0o111
    config = tmp_path / "services" / "windmill" / ".env"
    assert config.stat().st_mode & 0o777 == 0o600
    assert "SERVER_BIND_ADDR=0.0.0.0" in config.read_text(encoding="utf-8")
    config.write_text(
        "DATABASE_URL='postgres://windmill:test@localhost/windmill'\nMODE=standalone\n",
        encoding="utf-8",
    )
    configured = config.read_text(encoding="utf-8")
    windmill_service.install()
    assert config.read_text(encoding="utf-8") == configured

    called = {}
    monkeypatch.setattr(
        windmill_service,
        "start_background",
        lambda name, argv, env, directory: called.update(
            name=name, argv=argv, env=env, directory=directory
        ),
    )
    windmill_service.start()
    assert called["name"] == "windmill"
    assert called["argv"] == [str(executable)]
    assert called["directory"] == executable.parent
    assert called["env"]["MODE"] == "standalone"
    assert called["env"]["DATABASE_URL"] == (
        "postgres://windmill:test@localhost/windmill"
    )
    assert called["env"]["PORT"] == "8813"
    assert called["env"]["SERVER_BIND_ADDR"] == "0.0.0.0"

    monkeypatch.setenv("MODE", "worker")
    monkeypatch.setenv("WORKER_SUFFIX", "worker2")
    monkeypatch.delenv("PORT", raising=False)
    windmill_service.start()
    assert called["name"] == "windmill-worker2"
    assert "PORT" not in called["env"]


def test_windmill_default_url(monkeypatch):
    monkeypatch.delenv("WINDMILL_URL", raising=False)
    backend = WindmillBackend.from_env()
    try:
        assert str(backend.client.base_url) == "http://127.0.0.1:8813/api/w/admins/"
    finally:
        backend.close()


def test_windmill_health_check_hits_unauthenticated_status_endpoint():
    requests = []

    def handler(request: httpx.Request):
        requests.append(request)
        return httpx.Response(200, json={"status": "healthy"})

    client = httpx.Client(
        base_url="http://windmill/api/w/admins/",
        transport=httpx.MockTransport(handler),
    )
    backend = WindmillBackend("http://windmill", "admins", "token", client=client)

    backend.health_check()

    assert requests[0].url == "http://windmill/api/health/status"
    assert "Authorization" not in requests[0].headers


def test_windmill_health_check_fails_without_token():
    backend = WindmillBackend(
        "http://windmill", "admins", "", client=httpx.Client(base_url="http://windmill")
    )
    with pytest.raises(BackendError):
        backend.health_check()


def test_windmill_health_check_fails_on_unhealthy_status():
    client = httpx.Client(
        base_url="http://windmill/api/w/admins/",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"status": "unhealthy"})
        ),
    )
    backend = WindmillBackend("http://windmill", "admins", "token", client=client)
    with pytest.raises(BackendError):
        backend.health_check()


def test_dagu_translates_inline_dag_and_lifecycle():
    requests = []
    output_requests = 0

    def handler(request: httpx.Request):
        nonlocal output_requests
        requests.append(request)
        path = request.url.path
        if request.method == "POST" and path.endswith("/dag-runs"):
            return httpx.Response(200, json={"dagRunId": JOB_ID})
        if path.endswith(f"/{JOB_ID}/outputs"):
            output_requests += 1
            if output_requests == 1:
                return httpx.Response(200, json={"metadata": {}, "outputs": {}})
            return httpx.Response(
                200,
                json={
                    "metadata": {"status": "succeeded"},
                    "outputs": {"result": '{"a":"json:1","c":"text:3"}'},
                },
            )
        if path.endswith(f"/{JOB_ID}/reschedule"):
            return httpx.Response(200, json={"dagRunId": RERUN_ID})
        if path.endswith("/log"):
            stream = request.url.params.get("stream")
            return httpx.Response(
                200, json={"content": "A finished" if stream == "stdout" else ""}
            )
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "dagRunDetails": {
                        "statusLabel": "succeeded",
                        "startedAt": "2026-09-10T00:00:01Z",
                        "finishedAt": "2026-09-10T00:00:02Z",
                        "nodes": [
                            {
                                "statusLabel": "succeeded",
                                "step": {"id": "a", "name": "a"},
                            }
                        ],
                    }
                },
            )
        return httpx.Response(200)

    client = httpx.Client(
        base_url="http://dagu/api/v1/", transport=httpx.MockTransport(handler)
    )
    backend = DaguBackend("http://unused", client=client)
    result = backend.submit_workflow(
        workflow(
            depends_on=[RERUN_ID],
            callback_url="https://example.test/callback",
        )
    )
    task_id = result.task_id
    body = json.loads(requests[0].content)
    spec = json.loads(body["spec"])

    assert task_id == JOB_ID
    assert spec["name"] == "funmill"
    assert spec["steps"][0]["id"] == "funmill_wait"
    assert spec["steps"][1]["depends"] == ["funmill_wait"]
    compile(spec["steps"][1]["run"], "<dagu-test>", "exec")
    assert spec["steps"][2]["depends"] == ["a"]
    assert spec["steps"][-2]["action"] == "outputs.write"
    assert spec["steps"][-1]["depends"] == ["funmill_result"]
    assert set(spec["handler_on"]) == {"failure", "abort"}
    assert backend.get_task(JOB_ID).status == TaskStatus.SUCCEEDED
    assert backend.get_progress(JOB_ID).progress == 100
    assert backend.get_logs(JOB_ID).logs == "[a stdout]\nA finished"
    assert backend.get_result(JOB_ID).result == {"a": 1, "c": "3"}
    backend.cancel(JOB_ID, "stop")
    assert backend.rerun(JOB_ID).task_id == RERUN_ID


def test_dagu_task_name_and_description_round_trip():
    requests = []

    def handler(request: httpx.Request):
        requests.append(request)
        path = request.url.path
        if request.method == "POST" and path.endswith("/dag-runs"):
            return httpx.Response(200, json={"dagRunId": JOB_ID})
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "dagRunDetails": {
                        "statusLabel": "succeeded",
                        "nodes": [
                            {
                                "statusLabel": "succeeded",
                                "step": {
                                    "id": "task",
                                    "name": "Import customers",
                                    "description": "Nightly import job",
                                },
                            }
                        ],
                    }
                },
            )
        return httpx.Response(200)

    client = httpx.Client(
        base_url="http://dagu/api/v1/", transport=httpx.MockTransport(handler)
    )
    backend = DaguBackend("http://unused", client=client)
    result = backend.submit_task(
        TaskSubmit.model_validate(
            {
                "language": "python",
                "source": "def main(): pass",
                "name": "Import customers",
                "description": "Nightly import job",
            }
        )
    )
    body = json.loads(requests[0].content)
    spec = json.loads(body["spec"])
    assert spec["name"] == "Import customers"
    assert spec["steps"][0]["name"] == "Import customers"
    assert spec["steps"][0]["description"] == "Nightly import job"
    assert result.ui_url == f"http://unused/dag-runs/Import%20customers/{JOB_ID}"

    info = backend.get_task(result.task_id)
    assert info.name == "Import customers"
    assert info.description == "Nightly import job"
    # The mock always 200s regardless of path, so get_task takes the "funmill"
    # fast path rather than resolving the actual custom DAG name.
    assert info.ui_url == f"http://unused/dag-runs/funmill/{JOB_ID}"


def test_dagu_resolves_dag_name_via_list_filter_when_default_name_404s():
    # Proves two things through the real DaguBackend code path (not a
    # throwaway script): the DAG-level run name follows a custom task.name,
    # and names with characters that are unsafe to interpolate raw into a
    # URL path (space, "#", unicode) survive quoting end to end for every
    # operation that must resolve the run's actual Dagu name.
    custom_name = "每日客户导入 test#1"
    requests = []

    def handler(request: httpx.Request):
        requests.append(request)
        path = request.url.path
        method = request.method
        if method == "POST" and path == "/api/v1/dag-runs":
            return httpx.Response(200, json={"dagRunId": JOB_ID})
        if method == "GET" and path == "/api/v1/dag-runs":
            assert request.url.params.get("dagRunId") == JOB_ID
            return httpx.Response(200, json={"dagRuns": [{"name": custom_name}]})
        if path == f"/api/v1/dag-runs/funmill/{JOB_ID}":
            return httpx.Response(404, json={"detail": "not found"})
        if path == f"/api/v1/dag-runs/{custom_name}/{JOB_ID}":
            return httpx.Response(
                200,
                json={
                    "dagRunDetails": {
                        "statusLabel": "succeeded",
                        "nodes": [
                            {
                                "statusLabel": "succeeded",
                                "step": {"id": "task", "name": custom_name},
                            }
                        ],
                    }
                },
            )
        if path == f"/api/v1/dag-runs/{custom_name}/{JOB_ID}/steps/{custom_name}/log":
            stream = request.url.params.get("stream")
            return httpx.Response(
                200, json={"content": "done" if stream == "stdout" else ""}
            )
        if path == f"/api/v1/dag-runs/funmill/{JOB_ID}/stop":
            return httpx.Response(404, json={"detail": "not found"})
        if path == f"/api/v1/dag-runs/{custom_name}/{JOB_ID}/stop":
            return httpx.Response(200, json={})
        if path == f"/api/v1/dag-runs/funmill/{JOB_ID}/reschedule":
            return httpx.Response(404, json={"detail": "not found"})
        if path == f"/api/v1/dag-runs/{custom_name}/{JOB_ID}/reschedule":
            return httpx.Response(200, json={"dagRunId": RERUN_ID})
        raise AssertionError(f"unexpected request: {method} {path}")

    client = httpx.Client(
        base_url="http://dagu/api/v1/", transport=httpx.MockTransport(handler)
    )
    backend = DaguBackend("http://unused", client=client)
    result = backend.submit_task(
        TaskSubmit.model_validate(
            {"language": "python", "source": "def main(): pass", "name": custom_name}
        )
    )
    task_id = result.task_id
    submit_body = json.loads(requests[0].content)
    assert json.loads(submit_body["spec"])["name"] == custom_name

    info = backend.get_task(task_id)
    assert info.status == TaskStatus.SUCCEEDED
    assert info.name == custom_name

    logs = backend.get_logs(task_id)
    assert logs.logs == f"[{custom_name} stdout]\ndone"

    backend.cancel(task_id, "stop")
    rerun_result = backend.rerun(task_id)
    assert rerun_result.task_id == RERUN_ID
    assert rerun_result.ui_url == (
        f"http://unused/dag-runs/{quote(custom_name, safe='')}/{RERUN_ID}"
    )

    assert any(
        request.method == "GET" and request.url.path == "/api/v1/dag-runs"
        for request in requests
    )


def test_dagu_dependency_wait_script_resolves_named_dependency(monkeypatch):
    # The embedded wait-for-dependency script (executed inside a Dagu step,
    # calling Dagu's REST API directly) has the same "funmill" fast-path
    # guess as the backend itself. If the awaited dependency was itself
    # submitted with a custom name, the script must fall back to the same
    # list-filter resolution instead of 404ing forever.
    from urllib.error import HTTPError

    dependency_id = "33333333-3333-4333-8333-333333333333"
    custom_name = "nightly-import"
    calls = []

    class FakeResponse:
        def __init__(self, payload):
            self._payload = json.dumps(payload).encode()

        def read(self):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout=None):
        url = request.full_url
        calls.append(url)
        if url.endswith(f"/dag-runs/funmill/{dependency_id}"):
            raise HTTPError(url, 404, "not found", {}, None)
        if url.endswith(f"/dag-runs?dagRunId={dependency_id}"):
            return FakeResponse({"dagRuns": [{"name": custom_name}]})
        if url.endswith(f"/dag-runs/{custom_name}/{dependency_id}"):
            return FakeResponse({"dagRunDetails": {"statusLabel": "succeeded"}})
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    backend = DaguBackend("http://dagu")
    source = backend._dependency_step([dependency_id], 60)["run"]
    exec(compile(source, "<dependency-test>", "exec"), {"__name__": "__funmill_test__"})

    assert any(f"/dag-runs/funmill/{dependency_id}" in url for url in calls)
    assert any(f"/dag-runs?dagRunId={dependency_id}" in url for url in calls)
    assert any(f"/dag-runs/{custom_name}/{dependency_id}" in url for url in calls)


def test_dagu_task_without_name_leaves_name_and_description_none():
    def handler(request: httpx.Request):
        path = request.url.path
        if request.method == "POST" and path.endswith("/dag-runs"):
            return httpx.Response(200, json={"dagRunId": JOB_ID})
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "dagRunDetails": {
                        "statusLabel": "succeeded",
                        "nodes": [
                            {
                                "statusLabel": "succeeded",
                                "step": {"id": "task", "name": "task"},
                            }
                        ],
                    }
                },
            )
        return httpx.Response(200)

    client = httpx.Client(
        base_url="http://dagu/api/v1/", transport=httpx.MockTransport(handler)
    )
    backend = DaguBackend("http://unused", client=client)
    result = backend.submit_task(
        TaskSubmit.model_validate({"language": "python", "source": "def main(): pass"})
    )

    info = backend.get_task(result.task_id)
    assert info.name is None
    assert info.description is None


def test_dagu_health_check_reports_status():
    requests = []

    def handler(request: httpx.Request):
        requests.append(request)
        return httpx.Response(200, json={"status": "healthy"})

    client = httpx.Client(
        base_url="http://dagu/api/v1/", transport=httpx.MockTransport(handler)
    )
    backend = DaguBackend(
        "http://unused", username="funmill", password="funmill", client=client
    )

    backend.health_check()

    assert requests[0].url.path.endswith("/api/v1/health")
    expected = base64.b64encode(b"funmill:funmill").decode()
    assert requests[0].headers["Authorization"] == f"Basic {expected}"


def test_dagu_health_check_fails_on_unhealthy_status():
    client = httpx.Client(
        base_url="http://dagu/api/v1/",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"status": "unhealthy"})
        ),
    )
    backend = DaguBackend("http://unused", client=client)
    with pytest.raises(BackendError):
        backend.health_check()


def test_dagu_service_installs_via_pnpm_and_starts(monkeypatch, tmp_path):
    binary = b"dagu-test-binary"

    monkeypatch.setenv("FUNMILL_HOME", str(tmp_path))
    monkeypatch.delenv("DAGU_AUTH_MODE", raising=False)
    monkeypatch.delenv("DAGU_AUTH_BASIC_USERNAME", raising=False)
    monkeypatch.delenv("DAGU_AUTH_BASIC_PASSWORD", raising=False)
    monkeypatch.setattr(dagu_service.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(dagu_service.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(
        dagu_service.shutil,
        "which",
        lambda name: "/usr/local/bin/pnpm" if name == "pnpm" else None,
    )

    installed = {}

    def fake_run(argv, cwd, check):
        installed["argv"] = argv
        installed["cwd"] = Path(cwd)
        binary_path = (
            Path(cwd)
            / "node_modules"
            / "@dagucloud"
            / "dagu-darwin-arm64"
            / "bin"
            / "dagu"
        )
        binary_path.parent.mkdir(parents=True)
        binary_path.write_bytes(binary)

    monkeypatch.setattr(dagu_service.subprocess, "run", fake_run)

    executable = dagu_service.install()
    assert executable.read_bytes() == binary
    assert executable.stat().st_mode & 0o111
    assert installed["argv"] == ["pnpm", "install", "--prod", "--ignore-scripts"]
    package_json = json.loads((installed["cwd"] / "package.json").read_text())
    assert package_json["dependencies"] == {
        "@dagucloud/dagu-darwin-arm64": dagu_service.VERSION
    }

    called = {}
    monkeypatch.setattr(
        dagu_service,
        "start_background",
        lambda name, argv, env, directory: called.update(
            name=name, argv=argv, env=env, directory=directory
        ),
    )
    dagu_service.start()
    assert called["name"] == "dagu"
    assert called["directory"] == executable.parent
    assert called["argv"][called["argv"].index("--host") + 1] == "0.0.0.0"
    assert called["argv"][called["argv"].index("--port") + 1] == "8813"
    assert called["argv"][called["argv"].index("--coordinator.host") + 1] == "0.0.0.0"
    assert called["env"]["DAGU_AUTH_MODE"] == "basic"
    assert called["env"]["DAGU_AUTH_BASIC_USERNAME"] == "funmill"
    assert called["env"]["DAGU_AUTH_BASIC_PASSWORD"] == "funmill"
    assert called["env"]["DAGU_HOME"] == str(executable.parent / "data")
    assert called["env"]["DAGU_COORDINATOR_ENABLED"] == "false"

    executable.unlink()
    monkeypatch.setattr(
        dagu_service.shutil, "which", lambda _name: "/usr/local/bin/dagu"
    )
    dagu_service.start()
    dagu_home = tmp_path / "services" / "dagu" / "data"
    assert called["argv"][called["argv"].index("--dagu-home") + 1] == str(dagu_home)
    assert called["env"]["DAGU_HOME"] == str(dagu_home)


def test_dagu_service_force_install_requests_latest_and_clears_npm_dir(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("FUNMILL_HOME", str(tmp_path))
    monkeypatch.setattr(dagu_service.platform, "system", lambda: "Linux")
    monkeypatch.setattr(dagu_service.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        dagu_service.shutil,
        "which",
        lambda name: "/usr/local/bin/pnpm" if name == "pnpm" else None,
    )

    target = dagu_service._target()
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old-binary")
    stale_marker = dagu_service._npm_dir() / "stale-lockfile"
    stale_marker.parent.mkdir(parents=True)
    stale_marker.write_text("stale")

    def fake_run(argv, cwd, check):
        binary_path = (
            Path(cwd)
            / "node_modules"
            / "@dagucloud"
            / "dagu-linux-x64"
            / "bin"
            / "dagu"
        )
        binary_path.parent.mkdir(parents=True)
        binary_path.write_bytes(b"new-binary")

    monkeypatch.setattr(dagu_service.subprocess, "run", fake_run)

    executable = dagu_service.install(force=True)
    assert executable.read_bytes() == b"new-binary"
    assert not stale_marker.exists()
    package_json = json.loads((dagu_service._npm_dir() / "package.json").read_text())
    assert package_json["dependencies"] == {"@dagucloud/dagu-linux-x64": "latest"}


def test_third_party_service_starts_in_background(monkeypatch, tmp_path, capsys):
    called = {}

    class Process:
        pid = 123

        @staticmethod
        def wait(timeout):
            raise backend_service.subprocess.TimeoutExpired("demo", timeout)

    def popen(command, **kwargs):
        called.update(command=command, **kwargs)
        return Process()

    monkeypatch.setattr(backend_service.subprocess, "Popen", popen)
    backend_service.start_background("demo", ["/tmp/demo"], {"KEY": "value"}, tmp_path)

    assert called["command"] == ["/tmp/demo"]
    assert called["env"] == {"KEY": "value"}
    assert called["stdin"] is backend_service.subprocess.DEVNULL
    assert called["stderr"] is backend_service.subprocess.STDOUT
    assert called["start_new_session"] is True
    assert Path(called["stdout"].name) == tmp_path / "demo.log"
    assert called["stdout"].closed
    assert (tmp_path / "demo.pid").read_text(encoding="utf-8") == "123\n"
    assert "pid=123" in capsys.readouterr().out

    monkeypatch.setattr(backend_service, "_is_running", lambda _pid: True)
    with pytest.raises(RuntimeError, match="already running"):
        backend_service.start_background(
            "demo", ["/tmp/demo"], {"KEY": "value"}, tmp_path
        )
    assert backend_service.status_background("demo", tmp_path)

    running = iter([True, False])
    monkeypatch.setattr(backend_service, "_is_running", lambda _pid: next(running))
    monkeypatch.setattr(backend_service.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(
        backend_service.os,
        "killpg",
        lambda group_id, sig: called.update(group_id=group_id, signal=sig),
    )
    backend_service.stop_background("demo", tmp_path)
    assert called["group_id"] == 123
    assert called["signal"] is backend_service.signal.SIGTERM
    assert not (tmp_path / "demo.pid").exists()


def test_third_party_service_reports_startup_failure(monkeypatch, tmp_path):
    class Process:
        pid = 456

        @staticmethod
        def wait(timeout):
            return 2

    monkeypatch.setattr(
        backend_service.subprocess, "Popen", lambda *_args, **_kwargs: Process()
    )

    with pytest.raises(RuntimeError, match="exited during startup with code 2"):
        backend_service.start_background("demo", ["/tmp/demo"], {}, tmp_path)
    assert not (tmp_path / "demo.pid").exists()


def test_funmill_cli_run_uses_facade_port(monkeypatch):
    called = {}
    monkeypatch.setenv("FUNMILL_PORT", "9999")
    monkeypatch.setattr(
        "uvicorn.run",
        lambda app, **kwargs: called.update(app=app, **kwargs),
    )
    funmill_cli.main(["run"])
    assert called == {
        "app": "funmill.api:app",
        "host": "0.0.0.0",
        "port": 8812,
    }


def test_funmill_cli_starts_api_in_background(monkeypatch, tmp_path):
    monkeypatch.setenv("FUNMILL_HOME", str(tmp_path))
    called = {}
    monkeypatch.setattr(
        api_service,
        "start_background",
        lambda name, argv, env, directory: called.update(
            name=name, argv=argv, env=env, directory=directory
        ),
    )
    funmill_cli.main(["start"])
    assert called["name"] == "api"
    assert called["argv"] == [
        sys.executable,
        "-m",
        "uvicorn",
        "funmill.api:app",
        "--host",
        "0.0.0.0",
        "--port",
        "8812",
    ]
    assert called["directory"] == tmp_path / "services" / "api"


def test_funmill_cli_manages_third_party_service(monkeypatch):
    calls = []

    class Service:
        @staticmethod
        def start():
            calls.append("start")

        @staticmethod
        def stop():
            calls.append("stop")

        @staticmethod
        def status():
            calls.append("status")
            return True

    monkeypatch.setattr(funmill_cli, "_service", lambda _name: Service)
    funmill_cli.main(["status", "dagu"])
    funmill_cli.main(["stop", "dagu"])
    funmill_cli.main(["restart", "dagu"])
    assert calls == ["status", "stop", "stop", "start"]

    Service.status = staticmethod(lambda: False)
    with pytest.raises(SystemExit) as stopped:
        funmill_cli.main(["status", "dagu"])
    assert stopped.value.code == 1


def test_funmill_cli_defaults_service_management_to_api(monkeypatch):
    calls = []

    monkeypatch.setattr(api_service, "start", lambda: calls.append("start"))
    monkeypatch.setattr(api_service, "stop", lambda: calls.append("stop"))
    monkeypatch.setattr(api_service, "status", lambda: calls.append("status") or True)

    funmill_cli.main(["status"])
    funmill_cli.main(["stop"])
    funmill_cli.main(["restart"])
    assert calls == ["status", "stop", "stop", "start"]


class FakeBackend(TaskBackend):
    name = "fake"

    def health_check(self):
        return None

    def submit_task(self, task):
        assert task.language == "python"
        return SubmitResult(task_id=JOB_ID)

    def submit_workflow(self, workflow):
        return SubmitResult(task_id=JOB_ID)

    def get_task(self, task_id):
        return TaskInfo(task_id=task_id, status=TaskStatus.SUCCEEDED)

    def get_progress(self, task_id):
        return TaskProgress(task_id=task_id, progress=100)

    def get_logs(self, task_id):
        return TaskLogs(task_id=task_id, logs="done")

    def get_result(self, task_id):
        return TaskResult(task_id=task_id, result={"ok": True})

    def cancel(self, task_id, reason):
        return None

    def rerun(self, task_id):
        return SubmitResult(task_id=RERUN_ID)

    def close(self):
        return None


def test_api_is_backend_neutral_and_authenticated(monkeypatch):
    monkeypatch.setenv("FUNMILL_API_KEY", "secret")
    app.dependency_overrides[backend_dependency] = FakeBackend
    try:
        with TestClient(app) as client:
            assert (
                client.post(
                    "/v1/tasks",
                    json={"language": "python", "source": "def main(): pass"},
                ).status_code
                == 401
            )

            response = client.post(
                "/v1/tasks",
                headers={"X-API-Key": "secret"},
                json={"language": "python", "source": "def main(): pass"},
            )
            assert response.status_code == 202
            assert response.json() == {
                "task_id": JOB_ID,
                "status": "queued",
                "rerun_of": None,
                "logs_url": f"http://testserver/v1/tasks/{JOB_ID}/logs",
                "ui_url": None,
            }

            response = client.post(
                f"/v1/tasks/{JOB_ID}/rerun", headers={"X-API-Key": "secret"}
            )
            assert response.json()["task_id"] == RERUN_ID
            assert response.json()["rerun_of"] == JOB_ID
            assert (
                response.json()["logs_url"]
                == f"http://testserver/v1/tasks/{RERUN_ID}/logs"
            )
    finally:
        app.dependency_overrides.clear()


def test_health_endpoint_checks_backend_connectivity(monkeypatch):
    monkeypatch.setenv("FUNMILL_BACKEND", "dagu")
    app.dependency_overrides[backend_dependency] = FakeBackend
    try:
        with TestClient(app) as client:
            response = client.get("/health")
            assert response.status_code == 200
            assert response.json() == {"status": "ok", "backend": "dagu"}

        class UnhealthyBackend(FakeBackend):
            def health_check(self):
                raise BackendError("Dagu is unavailable", 502)

        app.dependency_overrides[backend_dependency] = UnhealthyBackend
        with TestClient(app) as client:
            response = client.get("/health")
            assert response.status_code == 502
            assert response.json() == {"detail": "Dagu is unavailable"}
    finally:
        app.dependency_overrides.clear()
