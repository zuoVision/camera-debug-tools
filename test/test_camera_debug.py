import copy
import io
import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

import camera_debug as app
from camera_debug_studio import config as config_module
from camera_debug_studio import jobs as jobs_module
from camera_debug_studio import http as http_module
from camera_debug_studio import monitoring as monitoring_module
from camera_debug_studio import terminal as terminal_module
from camera_debug_studio import transport as transport_module


def profile(password="secret"):
    return {
        "project": {"name": "test", "platform": "local"},
        "target": {"transport": "local", "localShell": "/bin/sh", "password": password},
        "variables": {"value": 42},
        "monitoring": {"metrics": [{
            "id": "value", "name": "Value", "command": "printf 42", "interval": 1,
            "timeout": 1, "parser": {"type": "number"},
        }]},
        "topology": {"nodes": [{"id": "camera", "x": 10, "y": 20}], "edges": []},
        "tests": [{"id": "echo", "name": "Echo", "command": "printf {value}",
                   "params": [{"name": "value", "pattern": "[A-Za-z0-9_-]+"}]}],
    }


def write_profile(root: Path, data=None):
    data = data or profile()
    for key, filename in app.MODULE_FILES.items():
        value = copy.deepcopy(data[key])
        if key == "target":
            value.pop("password", None)
        (root / filename).write_text(json.dumps(value), encoding="utf-8")
    (root / "target.local.json").write_text(json.dumps({"password": data["target"].get("password", "")}),
                                             encoding="utf-8")


@pytest.fixture
def runtime(tmp_path):
    write_profile(tmp_path)
    value = app.Runtime(tmp_path)
    yield value
    value.monitor_stop.set()
    value.monitor_pool.shutdown(wait=True, cancel_futures=True)


def wait_job(job, timeout=3):
    deadline = time.time() + timeout
    while job.status in ("queued", "running", "stopping") and time.time() < deadline:
        time.sleep(0.02)
    return job


@pytest.fixture
def api_server(runtime):
    previous_runtime = getattr(app, "RUNTIME", None)
    previous_token = app.ACCESS_TOKEN
    app.RUNTIME = runtime
    app.ACCESS_TOKEN = "test-access-token"
    server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    app.ACCESS_TOKEN = previous_token
    if previous_runtime is not None:
        app.RUNTIME = previous_runtime


def api_request(base_url, path, *, token="test-access-token", method="GET", body=None,
                headers=None):
    request_headers = dict(headers or {})
    if token is not None:
        request_headers["X-Access-Token"] = token
    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base_url + path, data=payload, headers=request_headers,
                                     method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        response = opener.open(request, timeout=3)
    except urllib.error.HTTPError as exc:
        response = exc
    content = response.read()
    return response.status, json.loads(content.decode("utf-8"))


def test_render_requires_all_parameters():
    assert app.render("x={value}", {"value": 3}) == "x=3"
    with pytest.raises(ValueError, match="缺少模板参数"):
        app.render("{missing}", {})


def test_compatibility_facade_exports_backend_building_blocks():
    assert app.Job is jobs_module.Job
    assert app.TerminalSession is terminal_module.TerminalSession
    assert app.MODULE_FILES is config_module.MODULE_FILES
    assert app.render is config_module.render
    assert app.load_profile is config_module.load_profile


def test_extracted_transport_and_metric_parser_characterization():
    local = {"target": {"transport": "local", "localShell": "/bin/sh"}}
    assert transport_module.transport_command(local, "printf ok") == [
        "/bin/sh", "-lc", "printf ok"]
    assert transport_module.terminal_command(local) == ["/bin/sh", "-l"]
    assert monitoring_module.parse_metric_output({"type": "number"}, "value=4.5") == 4.5
    assert monitoring_module.parse_metric_output(
        {"type": "regex", "pattern": "state=(\\w+)", "group": 1,
         "map": {"up": "ONLINE"}}, "state=up") == "ONLINE"


def test_extracted_http_protocol_helpers():
    assert http_module.token_authorized("a+b", "", "token=a%2Bb") is True
    assert http_module.token_authorized("secret", "wrong", "token=secret") is False
    frame = http_module.websocket_frame({"type": "ready"})
    assert frame[0] == 0x81
    assert json.loads(frame[2:].decode("utf-8")) == {"type": "ready"}

    payload = json.dumps({"type": "input", "data": "x"}).encode("utf-8")
    mask = b"abcd"
    masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    wire = bytes([0x81, 0x80 | len(payload)]) + mask + masked
    assert http_module.read_websocket_message(io.BytesIO(wire), 1024) == {
        "type": "input", "data": "x"}


def test_redaction_is_deep_and_preserves_source():
    source = {"target": {"password": "secret"}, "items": [{"token": "abc", "name": "ok"}]}
    safe = app.redact(source)
    assert safe["target"] == {"passwordConfigured": True}
    assert safe["items"] == [{"name": "ok"}]
    assert source["target"]["password"] == "secret"


def test_all_bundled_profiles_validate():
    for path in app.PROFILE_DIR.iterdir():
        if path.is_dir():
            app.Runtime.validate_config(app.load_profile(path))


def test_invalid_regex_duplicate_metric_and_topology_node_rejected():
    data = profile("")
    data["monitoring"]["metrics"][0]["parser"] = {"type": "regex", "pattern": "["}
    with pytest.raises(ValueError, match="正则无效"):
        app.Runtime.validate_config(data)
    data = profile("")
    data["monitoring"]["metrics"].append(copy.deepcopy(data["monitoring"]["metrics"][0]))
    with pytest.raises(ValueError, match="重复"):
        app.Runtime.validate_config(data)
    data = profile("")
    data["topology"]["edges"] = [{"from": "camera", "to": "missing"}]
    with pytest.raises(ValueError, match="不存在的节点"):
        app.Runtime.validate_config(data)


def test_test_parameter_rejects_shell_injection(runtime):
    with pytest.raises(ValueError, match="格式无效"):
        runtime.run_test("echo", {"value": "ok; touch /tmp/not-allowed"})


def test_local_job_success_and_cursor(runtime):
    job = wait_job(runtime.start_job("printf 'one\\ntwo\\n'"))
    assert job.status == "success"
    assert [line["text"] for line in job.public()["lines"]] == ["one", "two"]
    assert [line["text"] for line in job.public(1)["lines"]] == ["two"]


def test_job_timeout_and_stop(runtime):
    timed = wait_job(runtime.start_job("sleep 2", timeout=0.05))
    assert timed.status == "timed_out"
    running = runtime.start_job("sleep 5")
    deadline = time.time() + 1
    while running.status == "queued" and time.time() < deadline:
        time.sleep(0.01)
    assert runtime.stop_job(running.id)
    assert wait_job(running).status == "stopped"


def test_metric_result_has_diagnostics(runtime):
    result = runtime.query_metric(runtime.config["monitoring"]["metrics"][0])
    assert result["value"] == 42.0
    assert result["ok"] is True
    assert result["durationMs"] >= 0
    assert result["failureCount"] == 0


def test_save_preserves_password_and_creates_backup(runtime):
    data = runtime.safe_config()
    data.pop("configPath")
    runtime.save_config(data)
    assert runtime.config["target"]["password"] == "secret"
    runtime.save_config(data)
    assert (runtime.config_path / "project.json.bak").is_file()
    assert "secret" not in json.dumps(runtime.safe_config())


def test_profile_path_traversal_rejected(runtime):
    with pytest.raises(ValueError, match="文件名无效"):
        runtime.switch_profile("../outside")


def test_diagnostic_report_is_redacted(runtime):
    report = runtime.diagnostic_report()
    assert "secret" not in report
    assert app.VERSION in report


def test_diagnostic_session_records_bounded_redacted_job_and_metric_events(runtime):
    runtime.metrics["value"] = {"id": "value", "value": 41, "ok": True,
                                "updatedAt": app.now_ms(), "durationMs": 1,
                                "failureCount": 0, "errorCode": None}
    session = runtime.start_diagnostic_session("bring-up")
    assert session["status"] == "active"
    assert session["endedAt"] is None
    assert session["metricSnapshot"][0]["id"] == "value"
    with pytest.raises(app.ApiError) as duplicate:
        runtime.start_diagnostic_session("duplicate")
    assert duplicate.value.code == "diagnostic_session_active"

    job = wait_job(runtime.start_job("printf secret"))
    runtime._record_diagnostic_event("custom", {"password": "secret", "note": "secret"})
    for index in range(app.MAX_DIAGNOSTIC_EVENTS + 10):
        runtime._record_diagnostic_event("bounded", {"index": index})
    current = runtime.get_diagnostic_session()
    assert current is not None
    assert len(current["timeline"]) == app.MAX_DIAGNOSTIC_EVENTS
    serialized = json.dumps(current)
    assert "secret" not in serialized
    assert job.status == "success"

    ended = runtime.end_diagnostic_session()
    assert ended["status"] == "ended"
    assert ended["endedAt"] >= ended["startedAt"]
    assert "secret" not in runtime.diagnostic_session_report("json")
    markdown = runtime.diagnostic_session_report("markdown")
    assert "bring-up" in markdown
    assert runtime.clear_diagnostic_session() is True
    assert runtime.get_diagnostic_session() is None


def test_diagnostic_session_lifecycle_errors(runtime):
    with pytest.raises(app.ApiError) as missing:
        runtime.end_diagnostic_session()
    assert missing.value.code == "diagnostic_session_not_active"
    with pytest.raises(app.ApiError) as report_missing:
        runtime.diagnostic_session_report()
    assert report_missing.value.code == "diagnostic_session_not_found"
    runtime.start_diagnostic_session()
    with pytest.raises(app.ApiError, match="先结束"):
        runtime.clear_diagnostic_session()
    runtime.end_diagnostic_session()
    with pytest.raises(app.ApiError) as invalid_format:
        runtime.diagnostic_session_report("xml")
    assert invalid_format.value.code == "invalid_report_format"


def test_save_rejects_non_object_config(runtime):
    with pytest.raises(ValueError, match="config 必须是 JSON 对象"):
        runtime.save_config(None)


def test_config_preview_is_non_mutating_redacted_and_field_addressable(runtime):
    before = copy.deepcopy(runtime.config)
    preview = runtime.preview_config(runtime.safe_config())
    assert runtime.config == before
    assert preview["target"]["passwordConfigured"] is True
    assert "password" not in preview["target"]

    invalid = profile("")
    invalid["monitoring"]["metrics"][0]["parser"] = {"type": "regex", "pattern": "["}
    with pytest.raises(config_module.ConfigValidationError) as captured:
        runtime.preview_config(invalid)
    assert captured.value.details == {"path": "monitoring.metrics[0].parser.pattern"}


def test_copy_profile_is_atomic_public_only_and_rejects_unsafe_names(runtime, tmp_path,
                                                                     monkeypatch):
    profile_root = tmp_path / "profiles"
    profile_root.mkdir()
    source = profile_root / "source"
    source.mkdir()
    write_profile(source)
    monkeypatch.setattr(app, "PROFILE_DIR", profile_root)

    copied = runtime.copy_profile("source", "copy", "Copied profile")
    destination = profile_root / "copy"
    assert copied["file"] == "copy"
    assert copied["name"] == "Copied profile"
    assert destination.is_dir()
    assert not (destination / "target.local.json").exists()
    assert "secret" not in "".join(path.read_text(encoding="utf-8")
                                    for path in destination.glob("*.json"))
    assert "password" not in app.load_json(destination / "target.json")

    with pytest.raises(app.ApiError) as conflict:
        runtime.copy_profile("source", "copy")
    assert conflict.value.code == "profile_conflict"
    assert conflict.value.details == {"path": "file"}
    with pytest.raises(app.ApiError, match="名称无效"):
        runtime.copy_profile("../source", "escaped")


def test_job_limit_is_atomic(runtime, monkeypatch):
    monkeypatch.setattr(app, "MAX_CONCURRENT_JOBS", 1)
    release = threading.Event()
    original_run = runtime._run_job

    def delayed_run(job):
        release.wait(timeout=2)
        original_run(job)

    monkeypatch.setattr(runtime, "_run_job", delayed_run)
    first = runtime.start_job("sleep 0.1")
    with pytest.raises(app.ApiError, match="并发任务"):
        runtime.start_job("true")
    release.set()
    wait_job(first)


def test_old_metric_collection_cannot_overwrite_new_profile(runtime, monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def delayed_query(metric):
        entered.set()
        release.wait(timeout=2)
        return {"id": metric["id"], "value": "old", "ok": True, "updatedAt": app.now_ms()}

    monkeypatch.setattr(runtime, "query_metric", delayed_query)
    generation = runtime.config_generation
    worker = threading.Thread(target=runtime._collect_metric,
                              args=(copy.deepcopy(runtime.config["monitoring"]["metrics"][0]), generation))
    worker.start()
    assert entered.wait(timeout=1)
    runtime._replace_config(runtime.config_path, copy.deepcopy(runtime.config))
    release.set()
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert runtime.metrics == {}


def test_http_auth_and_structured_errors(api_server):
    status, payload = api_request(api_server, "/api/config", token=None)
    assert status == 401
    assert payload == {"error": {"code": "unauthorized", "message": "访问令牌无效",
                                  "details": None}}

    status, payload = api_request(api_server, "/api/config", token="wrong")
    assert status == 401
    assert payload["error"]["code"] == "unauthorized"

    status, payload = api_request(api_server, "/api/config")
    assert status == 200
    assert payload["target"]["passwordConfigured"] is True
    assert "password" not in payload["target"]

    status, payload = api_request(api_server, "/api/does-not-exist", method="POST", body={})
    assert status == 404
    assert payload["error"]["code"] == "not_found"


def test_http_config_validation_returns_field_path(api_server):
    invalid = profile("")
    invalid["tests"][0]["timeout"] = -1
    status, payload = api_request(api_server, "/api/config/validate", method="POST",
                                  body={"config": invalid})
    assert status == 400
    assert payload["error"]["code"] == "invalid_config"
    assert payload["error"]["details"] == {"path": "tests[0].timeout"}

    status, payload = api_request(api_server, "/api/config/validate", method="POST",
                                  body={"config": profile()})
    assert status == 200
    assert payload["valid"] is True
    assert "password" not in payload["config"]["target"]


def test_http_profile_copy_contract(api_server, tmp_path, monkeypatch):
    profile_root = tmp_path / "profiles"
    profile_root.mkdir()
    source = profile_root / "source"
    source.mkdir()
    write_profile(source)
    monkeypatch.setattr(app, "PROFILE_DIR", profile_root)

    status, payload = api_request(api_server, "/api/config/profiles/copy", method="POST",
                                  body={"sourceFile": "source", "file": "copy",
                                        "name": "HTTP copy"})
    assert status == 201
    assert payload == {"ok": True, "file": "copy", "profile": {
        "file": "copy", "name": "HTTP copy", "platform": "local"}}
    assert not (profile_root / "copy" / "target.local.json").exists()

    status, payload = api_request(api_server, "/api/config/profiles/copy", method="POST",
                                  body={"sourceFile": "source", "file": "../escape"})
    assert status == 400
    assert payload["error"]["code"] == "invalid_profile_name"
    assert payload["error"]["details"] == {"path": "file"}


def test_http_invalid_json_and_request_size_limit(api_server, monkeypatch):
    request = urllib.request.Request(
        api_server + "/api/commands", data=b"{", method="POST",
        headers={"X-Access-Token": "test-access-token", "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with pytest.raises(urllib.error.HTTPError) as captured:
        opener.open(request, timeout=3)
    payload = json.loads(captured.value.read().decode("utf-8"))
    assert captured.value.code == 400
    assert payload["error"]["code"] == "bad_request"

    monkeypatch.setattr(app, "MAX_BODY_BYTES", 8)
    status, payload = api_request(api_server, "/api/commands", method="POST",
                                  body={"command": "printf too-large"})
    assert status == 400
    assert payload["error"]["code"] == "request_too_large"


def test_http_job_lifecycle_and_log_cursor(api_server):
    status, created = api_request(api_server, "/api/commands", method="POST",
                                  body={"command": "printf 'one\\ntwo\\n'"})
    assert status == 201
    assert created["status"] in ("queued", "running", "success")
    job_id = created["id"]

    deadline = time.time() + 3
    current = created
    while current["status"] in ("queued", "running", "stopping") and time.time() < deadline:
        time.sleep(0.02)
        status, current = api_request(api_server, f"/api/jobs/{job_id}")
        assert status == 200
    assert current["status"] == "success"
    assert current["createdAt"] == current["startedAt"]
    assert current["durationMs"] >= 0
    assert [line["text"] for line in current["lines"]] == ["one", "two"]

    status, tail = api_request(api_server, f"/api/jobs/{job_id}?after=1")
    assert status == 200
    assert [line["text"] for line in tail["lines"]] == ["two"]

    status, missing = api_request(api_server, "/api/jobs/not-found")
    assert status == 404
    assert missing["error"]["code"] == "job_not_found"


def test_http_stop_job_and_clear_history(api_server):
    _, created = api_request(api_server, "/api/commands", method="POST",
                             body={"command": "sleep 5"})
    job_id = created["id"]
    status, result = api_request(api_server, f"/api/jobs/{job_id}/stop", method="POST", body={})
    assert status == 200
    assert result["stopped"] is True

    deadline = time.time() + 3
    current = created
    while current["status"] in ("queued", "running", "stopping") and time.time() < deadline:
        time.sleep(0.02)
        _, current = api_request(api_server, f"/api/jobs/{job_id}")
    assert current["status"] == "stopped"
    assert current["stopReason"] == "user"

    status, result = api_request(api_server, "/api/jobs/clear", method="POST", body={})
    assert status == 200
    assert result["cleared"] >= 1


def test_http_diagnostic_session_full_lifecycle_and_export(api_server):
    status, empty = api_request(api_server, "/api/diagnostics/session")
    assert status == 200
    assert empty == {"session": None}

    status, created = api_request(api_server, "/api/diagnostics/session/start", method="POST",
                                  body={"name": "API session"})
    assert status == 201
    assert created["session"]["status"] == "active"

    _, job = api_request(api_server, "/api/commands", method="POST",
                         body={"command": "printf done"})
    deadline = time.time() + 3
    while job["status"] in ("queued", "running", "stopping") and time.time() < deadline:
        time.sleep(0.02)
        _, job = api_request(api_server, f"/api/jobs/{job['id']}")

    status, ended = api_request(api_server, "/api/diagnostics/session/end", method="POST",
                                body={})
    assert status == 200
    session = ended["session"]
    assert session["status"] == "ended"
    event_types = [event["type"] for event in session["timeline"]]
    assert "job_created" in event_types
    assert "job_finished" in event_types

    status, exported = api_request(api_server,
                                   "/api/diagnostics/session/report?format=markdown")
    assert status == 200
    assert exported["format"] == "markdown"
    assert "API session" in exported["content"]

    status, cleared = api_request(api_server, "/api/diagnostics/session/clear", method="POST",
                                  body={})
    assert status == 200
    assert cleared == {"cleared": True}


def test_metric_pool_isolates_slow_metric(runtime):
    slow = {"id": "slow", "name": "Slow", "command": "sleep 0.4; printf 1",
            "interval": 1, "timeout": 1, "parser": {"type": "number"}}
    fast = {"id": "fast", "name": "Fast", "command": "printf 2",
            "interval": 1, "timeout": 1, "parser": {"type": "number"}}
    runtime.config["monitoring"]["metrics"] = [slow, fast]

    started = time.monotonic()
    assert runtime.refresh_metrics() == 2
    deadline = time.time() + 1
    while "fast" not in runtime.metrics and time.time() < deadline:
        time.sleep(0.01)
    fast_elapsed = time.monotonic() - started

    assert runtime.metrics["fast"]["value"] == 2.0
    assert fast_elapsed < 0.3
    # The completed fast metric may run again, but the still-running slow metric
    # must not be submitted a second time.
    assert runtime.refresh_metrics() == 1

    deadline = time.time() + 2
    while "slow" not in runtime.metrics and time.time() < deadline:
        time.sleep(0.01)
    assert runtime.metrics["slow"]["value"] == 1.0


def test_metric_failure_count_stale_and_failed_only_retry(runtime):
    metric = {"id": "broken", "name": "Broken", "command": "exit 2", "interval": 0.1,
              "timeout": 1, "staleAfter": 0.1, "parser": {"type": "text"}}
    runtime.config["monitoring"]["metrics"] = [metric]
    first = runtime.query_metric(metric)
    runtime.metrics["broken"] = first
    second = runtime.query_metric(metric)
    assert first["errorCode"] == "nonzero_exit"
    assert second["failureCount"] == 2
    runtime.metrics["broken"] = second
    runtime.metrics["broken"]["updatedAt"] = app.now_ms() - 1000
    assert runtime.public_metrics()[0]["stale"] is True
    assert runtime.refresh_metrics(failed_only=True) == 1


def test_http_metric_refresh_supports_group_and_reports_schedule(api_server):
    app.RUNTIME.config["monitoring"]["metrics"] = [
        {"id": "front", "name": "Front", "group": "FRONT", "command": "sleep 0.1; printf 1",
         "interval": 1, "timeout": 1, "parser": {"type": "number"}},
        {"id": "rear", "name": "Rear", "group": "REAR", "command": "printf 2",
         "interval": 1, "timeout": 1, "parser": {"type": "number"}},
    ]
    status, payload = api_request(api_server, "/api/metrics/control", method="POST",
                                  body={"action": "refresh", "group": "FRONT"})
    assert status == 200
    assert payload["scheduled"] == 1

    status, payload = api_request(api_server, "/api/metrics/control", method="POST",
                                  body={"action": "refresh", "group": ["FRONT"]})
    assert status == 400
    assert payload["error"]["code"] == "invalid_metric_group"
