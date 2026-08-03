#!/usr/bin/env python3
"""Camera Debug Studio - dependency-free local web service."""

from __future__ import annotations

import argparse
import base64
import copy
import errno
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from camera_debug_studio.config import (
    ConfigValidationError, MODULE_FILES, SENSITIVE_KEYS, load_json, load_profile, redact, render,
    validate_config as validate_profile_config, write_json,
)
from camera_debug_studio.jobs import Job, MAX_JOB_LINES, STATE_LOCK, now_ms
from camera_debug_studio.http import read_websocket_message, token_authorized, websocket_frame
from camera_debug_studio.monitoring import apply_parser_transforms, parse_metric_output
from camera_debug_studio.terminal import TerminalSession
from camera_debug_studio.transport import (
    command_environment as build_command_environment,
    terminal_command as build_terminal_command,
    transport_command as build_transport_command,
)


ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
CONFIG_DIR = ROOT / "configs"
PROFILE_DIR = CONFIG_DIR / "profiles"
TEST_DIR = ROOT / "test"
VERSION = "0.2.0"
MAX_BODY_BYTES = 1024 * 1024
MAX_WS_BYTES = 1024 * 1024
MAX_JOBS = 200
MAX_CONCURRENT_JOBS = 8
JOB_RETENTION_MS = 24 * 60 * 60 * 1000
MAX_DIAGNOSTIC_EVENTS = 500


class ApiError(ValueError):
    def __init__(self, message: str, code: str = "bad_request", details: Any = None):
        super().__init__(message)
        self.code = code
        self.details = details


class Runtime:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config = load_profile(config_path)
        self.validate_config(self.config)
        self.jobs: Dict[str, Job] = {}
        self.terminal_sessions: set[TerminalSession] = set()
        self.metrics: Dict[str, Dict[str, Any]] = {}
        self.monitor_stop = threading.Event()
        self.monitor_thread: Optional[threading.Thread] = None
        self.monitor_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="metric")
        self.metric_running: set[str] = set()
        self.config_generation = 0
        self.monitor_paused = False
        self.job_slots = threading.BoundedSemaphore(MAX_CONCURRENT_JOBS)
        self.recent_errors: List[Dict[str, Any]] = []
        self.diagnostic_session: Optional[Dict[str, Any]] = None
        self.started_at = now_ms()

    def reload(self) -> None:
        data = load_profile(self.config_path)
        self.validate_config(data)
        self._replace_config(self.config_path, data)

    def record_error(self, area: str, message: str, code: str = "runtime_error") -> None:
        with STATE_LOCK:
            self.recent_errors.append({"time": now_ms(), "area": area, "code": code,
                                       "message": str(message)[:500]})
            del self.recent_errors[:-50]

    def close_sessions(self) -> None:
        with STATE_LOCK:
            sessions = list(self.terminal_sessions)
        for session in sessions:
            session.close()

    def _replace_config(self, path: Path, data: Dict[str, Any]) -> None:
        self.close_sessions()
        with STATE_LOCK:
            self.config_generation += 1
            self.config_path = path
            self.config = data
            self.metrics.clear()
            self.metric_running.clear()
        self._record_diagnostic_event("profile_changed", {
            "profile": data.get("project", {}).get("name", path.name),
            "transport": data.get("target", {}).get("transport", "ssh"),
        })

    def safe_config(self) -> Dict[str, Any]:
        safe = redact(copy.deepcopy(self.config))
        safe["configPath"] = str(self.config_path)
        return safe

    def profiles(self) -> List[Dict[str, Any]]:
        result = []
        for path in sorted(PROFILE_DIR.glob("*")):
            if not path.is_dir():
                continue
            try:
                data = load_profile(path)
                result.append({"file": path.name, "name": data.get("project", {}).get("name", path.name),
                               "platform": data.get("project", {}).get("platform", "通用"),
                               "active": path.resolve() == self.config_path})
            except (OSError, ValueError):
                continue
        for path in sorted(CONFIG_DIR.glob("*.json")):
            try:
                data = load_json(path)
                result.append({"file": path.name, "name": data.get("project", {}).get("name", path.stem),
                               "platform": data.get("project", {}).get("platform", "通用"),
                               "active": path.resolve() == self.config_path})
            except (OSError, ValueError):
                continue
        return result

    def switch_profile(self, filename: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", filename):
            raise ValueError("配置文件名无效")
        modular = (PROFILE_DIR / filename).resolve()
        legacy = (CONFIG_DIR / filename).resolve()
        target_path = modular if modular.is_dir() else legacy
        if CONFIG_DIR.resolve() not in target_path.parents or not (target_path.is_dir() or target_path.is_file()):
            raise ValueError("配置文件不存在")
        data = load_profile(target_path)
        self.validate_config(data)
        self._replace_config(target_path, data)

    @staticmethod
    def validate_config(data: Dict[str, Any]) -> None:
        return validate_profile_config(data)

    def save_config(self, data: Dict[str, Any]) -> None:
        if not isinstance(data, dict):
            raise ValueError("config 必须是 JSON 对象")
        data = copy.deepcopy(data)
        incoming_target = data.get("target", {})
        if not isinstance(incoming_target, dict):
            raise ValueError("target 必须是 JSON 对象")
        incoming_target.pop("passwordConfigured", None)
        if "password" not in incoming_target and self.config.get("target", {}).get("password"):
            incoming_target["password"] = self.config["target"]["password"]
        self.validate_config(data)
        if self.config_path.is_dir():
            for key, filename in MODULE_FILES.items():
                value = data.get(key, {} if key != "tests" else [])
                if key == "target":
                    self.write_target(value)
                else:
                    write_json(self.config_path / filename, value)
        else:
            write_json(self.config_path, data)
        self._replace_config(self.config_path, data)

    def preview_config(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Validate an editor payload without changing files or runtime state."""
        if not isinstance(data, dict):
            raise ConfigValidationError("config 必须是 JSON 对象", "$")
        candidate = copy.deepcopy(data)
        candidate.pop("configPath", None)
        incoming_target = candidate.get("target")
        if not isinstance(incoming_target, dict):
            raise ConfigValidationError("target 必须是 JSON 对象", "target")
        incoming_target.pop("passwordConfigured", None)
        if "password" not in incoming_target and self.config.get("target", {}).get("password"):
            incoming_target["password"] = self.config["target"]["password"]
        self.validate_config(candidate)
        return redact(candidate)

    def copy_profile(self, source: str, destination: str, display_name: str = "") -> Dict[str, Any]:
        """Create a credential-free modular profile using an atomic directory rename."""
        name_pattern = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"
        if not re.fullmatch(name_pattern, source or ""):
            raise ApiError("源 Profile 名称无效", "invalid_profile_name", {"path": "sourceFile"})
        if not re.fullmatch(name_pattern, destination or "") or destination in (".", ".."):
            raise ApiError("目标 Profile 名称无效", "invalid_profile_name", {"path": "file"})
        source_path = (PROFILE_DIR / source).resolve()
        destination_path = (PROFILE_DIR / destination).resolve()
        profile_root = PROFILE_DIR.resolve()
        if profile_root not in source_path.parents or not source_path.is_dir():
            raise ApiError("源 Profile 不存在", "profile_not_found", {"path": "sourceFile"})
        if profile_root not in destination_path.parents:
            raise ApiError("目标 Profile 路径无效", "invalid_profile_name", {"path": "file"})
        temporary_path: Optional[Path] = None
        with STATE_LOCK:
            if destination_path.exists():
                raise ApiError("目标 Profile 已存在", "profile_conflict", {"path": "file"})
            temporary_path = Path(tempfile.mkdtemp(prefix=f".{destination}.tmp-", dir=str(PROFILE_DIR)))
            try:
                for key, filename in MODULE_FILES.items():
                    source_file = source_path / filename
                    if source_file.is_file():
                        value = redact(load_json(source_file))
                    elif key in ("variables", "monitoring", "topology", "tests"):
                        value = {"variables": {}, "monitoring": {"metrics": []},
                                 "topology": {"nodes": [], "edges": []}, "tests": []}[key]
                    else:
                        raise ApiError(f"源 Profile 缺少 {filename}", "invalid_source_profile")
                    if key == "target" and isinstance(value, dict):
                        value.pop("passwordConfigured", None)
                    if key == "project" and display_name:
                        if not isinstance(value, dict):
                            raise ApiError("project.json 必须是 JSON 对象", "invalid_source_profile")
                        value["name"] = str(display_name)[:100]
                    write_json(temporary_path / filename, value)
                self.validate_config(load_profile(temporary_path))
                temporary_path.rename(destination_path)
                temporary_path = None
            finally:
                if temporary_path and temporary_path.exists():
                    for child in temporary_path.iterdir():
                        child.unlink()
                    temporary_path.rmdir()
        copied = load_profile(destination_path)
        return {"file": destination, "name": copied.get("project", {}).get("name", destination),
                "platform": copied.get("project", {}).get("platform", "通用")}

    def write_target(self, target: Dict[str, Any]) -> None:
        if not self.config_path.is_dir():
            return
        public_target = dict(target)
        password = str(public_target.pop("password", ""))
        write_json(self.config_path / MODULE_FILES["target"], public_target)
        local_target = self.config_path / "target.local.json"
        if password or local_target.exists():
            write_json(local_target, {"password": password})
            try:
                local_target.chmod(0o600)
            except OSError:
                pass

    def update_target(self, values: Dict[str, Any]) -> Dict[str, Any]:
        mode = str(values.get("transport", "ssh"))
        if mode not in ("ssh", "local"):
            raise ValueError("transport 只能是 ssh 或 local")
        target = dict(self.config.get("target", {}))
        target["transport"] = mode
        if mode == "ssh":
            host = str(values.get("host", "")).strip()
            user = str(values.get("user", "")).strip()
            identity = str(values.get("identityFile", "")).strip()
            if not host or not re.fullmatch(r"[A-Za-z0-9._:-]+", host):
                raise ValueError("SSH 主机地址无效")
            if user and not re.fullmatch(r"[A-Za-z0-9._-]+", user):
                raise ValueError("SSH 用户名无效")
            try:
                port = int(values.get("port", 22))
                timeout = int(values.get("connectTimeout", 8))
            except (TypeError, ValueError):
                raise ValueError("端口和超时时间必须是整数")
            if not 1 <= port <= 65535 or not 1 <= timeout <= 120:
                raise ValueError("端口或超时时间超出范围")
            target.update({"host": host, "user": user, "port": port,
                           "connectTimeout": timeout, "identityFile": identity})
            supplied_password = str(values.get("password", ""))
            if supplied_password:
                target["password"] = supplied_password
            elif values.get("clearPassword"):
                target["password"] = ""
            target["sshOptions"] = ["StrictHostKeyChecking=accept-new"]
        else:
            target["localShell"] = str(values.get("localShell", target.get("localShell", "/bin/sh")))
        data = dict(self.config)
        data["target"] = target
        if self.config_path.is_dir():
            self.write_target(target)
        else:
            write_json(self.config_path, data)
        self._replace_config(self.config_path, data)
        return redact(target)

    def command_environment(self) -> Dict[str, str]:
        return build_command_environment(self.config, ROOT)

    def transport_command(self, remote_command: str) -> List[str]:
        return build_transport_command(self.config, remote_command)

    def terminal_command(self) -> List[str]:
        """Build a persistent interactive shell command for a PTY session."""
        return build_terminal_command(self.config)

    def _prune_jobs(self) -> None:
        cutoff = now_ms() - JOB_RETENTION_MS
        with STATE_LOCK:
            finished = [job for job in self.jobs.values() if job.ended_at]
            remove = {job.id for job in finished if (job.ended_at or 0) < cutoff}
            for job in sorted(finished, key=lambda item: item.ended_at or 0)[:-MAX_JOBS]:
                remove.add(job.id)
            for job_id in remove:
                self.jobs.pop(job_id, None)

    def start_job(self, command: str, kind: str = "command", name: str = "命令",
                  timeout: Optional[float] = None,
                  expected_exit_codes: Optional[List[int]] = None,
                  argv: Optional[List[str]] = None, cwd: Optional[str] = None) -> Job:
        self._prune_jobs()
        with STATE_LOCK:
            active = sum(job.status in ("queued", "running", "stopping") for job in self.jobs.values())
            if active >= MAX_CONCURRENT_JOBS:
                raise ApiError("并发任务已达到上限，请等待当前任务结束", "job_limit")
            job = Job(uuid.uuid4().hex[:12], kind, name, command, timeout=timeout,
                      expected_exit_codes=expected_exit_codes or [0], argv=argv, cwd=cwd)
            self.jobs[job.id] = job
        self._record_diagnostic_event("job_created", {
            "jobId": job.id, "kind": job.kind, "name": job.name,
            "status": job.status, "command": job.command,
        })
        threading.Thread(target=self._run_job, args=(job,), daemon=True).start()
        return job

    def collect_pytest(self) -> Dict[str, Any]:
        TEST_DIR.mkdir(exist_ok=True)
        python = self.pytest_python()
        argv = [python, "-m", "pytest", "--collect-only", "-q", TEST_DIR.name]
        try:
            result = subprocess.run(argv, cwd=str(ROOT), capture_output=True, text=True,
                                    timeout=30, errors="replace")
        except subprocess.TimeoutExpired:
            raise ValueError("Pytest 用例收集超时（30 秒）")
        output = (result.stdout + "\n" + result.stderr).strip()
        if "No module named pytest" in output:
            raise ValueError(f"Pytest Python 未安装 pytest，请执行：{python} -m pip install pytest")
        node_ids = []
        for line in result.stdout.splitlines():
            node_id = line.strip()
            if "::" in node_id and node_id.startswith(("test/", "test\\")):
                node_ids.append(node_id.replace("\\", "/"))
        return {"items": node_ids, "count": len(node_ids), "output": output,
                "available": result.returncode in (0, 5)}

    def start_pytest(self, node_id: str) -> Job:
        collected = self.collect_pytest()
        if node_id not in collected["items"]:
            raise ValueError("未知或已变化的 Pytest 用例，请刷新用例列表")
        argv = [self.pytest_python(), "-m", "pytest", "-v", "--color=no", node_id]
        return self.start_job(" ".join(argv), "pytest", f"Pytest · {node_id}",
                              argv=argv, cwd=str(ROOT))

    @staticmethod
    def pytest_python() -> str:
        configured = os.environ.get("CAMERA_DEBUG_PYTEST_PYTHON", "").strip()
        candidates = [configured] if configured else []
        if os.name == "nt":
            candidates += [str(TEST_DIR / ".venv" / "Scripts" / "python.exe"),
                           str(ROOT / ".venv" / "Scripts" / "python.exe")]
        else:
            candidates += [str(TEST_DIR / ".venv" / "bin" / "python"),
                           str(ROOT / ".venv" / "bin" / "python")]
        return next((path for path in candidates if path and Path(path).is_file()), sys.executable)

    def _run_job(self, job: Job) -> None:
        acquired = self.job_slots.acquire(timeout=1)
        if not acquired:
            job.status = "failed"
            job.stop_reason = "concurrency_limit"
            job.append("stderr", "并发任务槽位不可用")
            job.ended_at = now_ms()
            self._record_diagnostic_event("job_finished", {
                "jobId": job.id, "kind": job.kind, "name": job.name,
                "status": job.status, "exitCode": job.exit_code,
                "stopReason": job.stop_reason,
                "durationMs": job.ended_at - job.started_at,
            })
            return
        try:
            if job.status == "stopping":
                job.status = "stopped"
                return
            argv = job.argv or self.transport_command(job.command)
            flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            process = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1, errors="replace", creationflags=flags,
                start_new_session=(os.name != "nt"), env=self.command_environment(),
                cwd=job.cwd,
            )
            job.process = process
            job.status = "running"
            def pump(stream: Any, label: str) -> None:
                for line in iter(stream.readline, ""):
                    job.append(label, line.rstrip("\r\n"))
                stream.close()
            threads = [
                threading.Thread(target=pump, args=(process.stdout, "stdout"), daemon=True),
                threading.Thread(target=pump, args=(process.stderr, "stderr"), daemon=True),
            ]
            for thread in threads: thread.start()
            try:
                code = process.wait(timeout=job.timeout)
            except subprocess.TimeoutExpired:
                job.status = "timed_out"
                job.stop_reason = "timeout"
                self._terminate_process(job, force=False)
                try:
                    code = process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._terminate_process(job, force=True)
                    code = process.wait()
            for thread in threads: thread.join(timeout=1)
            job.exit_code = code
            if job.status == "stopping":
                job.status = "stopped"
            elif job.status != "timed_out":
                job.status = "success" if code in job.expected_exit_codes else "failed"
        except Exception as exc:
            job.append("stderr", str(exc))
            job.status = "failed"
            job.exit_code = -1
        finally:
            job.ended_at = now_ms()
            job.process = None
            self._record_diagnostic_event("job_finished", {
                "jobId": job.id, "kind": job.kind, "name": job.name,
                "status": job.status, "exitCode": job.exit_code,
                "stopReason": job.stop_reason,
                "durationMs": job.ended_at - job.started_at,
            })
            self.job_slots.release()

    @staticmethod
    def _terminate_process(job: Job, force: bool) -> None:
        if not job.process:
            return
        try:
            if os.name == "nt":
                job.process.kill() if force else job.process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(job.process.pid, signal.SIGKILL if force else signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass

    def stop_job(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if not job or job.status not in ("queued", "running"):
            return False
        job.status = "stopping"
        job.stop_reason = "user"
        if not job.process:
            return True
        self._terminate_process(job, force=False)
        def force_later() -> None:
            time.sleep(2)
            if job.process and job.process.poll() is None:
                self._terminate_process(job, force=True)
        threading.Thread(target=force_later, daemon=True).start()
        return True

    def run_test(self, test_id: str, params: Dict[str, Any]) -> Job:
        test = next((x for x in self.config.get("tests", []) if x.get("id") == test_id), None)
        if not test:
            raise ValueError("未知测试项")
        validated: Dict[str, Any] = {}
        for spec in test.get("params", []):
            key = spec["name"]
            value = params.get(key, spec.get("default", ""))
            if spec.get("type") == "number":
                if not re.fullmatch(r"[-+]?\d+(?:\.\d+)?", str(value)):
                    raise ValueError(f"参数 {key} 必须是数字")
            else:
                pattern = spec.get("pattern", r"[A-Za-z0-9_.:/@+-]*")
                if not re.fullmatch(pattern, str(value)):
                    raise ValueError(f"参数 {key} 格式无效")
            choices = spec.get("choices")
            if choices is not None and value not in choices:
                raise ValueError(f"参数 {key} 不在允许范围内")
            validated[key] = value
        command = render(test["command"], validated)
        precheck = str(test.get("precheck", "")).strip()
        if precheck:
            result = subprocess.run(self.transport_command(render(precheck, validated)), capture_output=True,
                                    text=True, timeout=min(float(test.get("timeout", 10) or 10), 30),
                                    errors="replace", env=self.command_environment())
            if result.returncode != 0:
                raise ApiError((result.stderr or result.stdout or "测试前置检查失败").strip(), "precheck_failed")
        return self.start_job(command, "test", test.get("name", test_id),
                              float(test.get("timeout")) if test.get("timeout") else None,
                              test.get("expectedExitCodes", [0]))

    def query_metric(self, metric: Dict[str, Any]) -> Dict[str, Any]:
        command = render(metric["command"], self.config.get("variables", {}))
        started = now_ms()
        previous = self.metrics.get(metric["id"], {})
        try:
            result = subprocess.run(self.transport_command(command), capture_output=True, text=True,
                                    timeout=float(metric.get("timeout", 5)), errors="replace",
                                    env=self.command_environment())
            output = (result.stdout + "\n" + result.stderr).strip()
            parser = metric.get("parser", {"type": "text"})
            value = parse_metric_output(parser, output)
            ok = result.returncode == 0
            return {"id": metric["id"], "value": value, "ok": ok, "raw": output,
                    "durationMs": now_ms() - started, "failureCount": 0 if ok else int(previous.get("failureCount", 0)) + 1,
                    "errorCode": None if ok else "nonzero_exit", "updatedAt": now_ms()}
        except subprocess.TimeoutExpired as exc:
            return {"id": metric["id"], "value": "--", "ok": False, "error": str(exc),
                    "errorCode": "timeout", "durationMs": now_ms() - started,
                    "failureCount": int(previous.get("failureCount", 0)) + 1, "updatedAt": now_ms()}
        except Exception as exc:
            self.record_error("metric", f"{metric['id']}: {exc}", "metric_error")
            return {"id": metric["id"], "value": "--", "ok": False,
                    "error": str(exc), "errorCode": "parse_or_transport", "durationMs": now_ms() - started,
                    "failureCount": int(previous.get("failureCount", 0)) + 1, "updatedAt": now_ms()}

    def _collect_metric(self, metric: Dict[str, Any], generation: int) -> None:
        key = metric["id"]
        accepted = False
        try:
            result = self.query_metric(metric)
            with STATE_LOCK:
                if generation == self.config_generation:
                    self.metrics[key] = result
                    accepted = True
            if accepted:
                self._record_diagnostic_event("metric_snapshot", {
                    "metricId": key, "value": result.get("value"),
                    "ok": result.get("ok", False), "errorCode": result.get("errorCode"),
                    "durationMs": result.get("durationMs"),
                    "failureCount": result.get("failureCount", 0),
                })
        finally:
            with STATE_LOCK:
                if generation == self.config_generation:
                    self.metric_running.discard(key)

    def start_monitor(self) -> None:
        if self.monitor_thread and self.monitor_thread.is_alive():
            return
        self.monitor_stop.clear()
        def loop() -> None:
            next_run: Dict[str, float] = {}
            while not self.monitor_stop.is_set():
                if self.monitor_paused:
                    self.monitor_stop.wait(0.2)
                    continue
                for metric in list(self.config.get("monitoring", {}).get("metrics", [])):
                    if not metric.get("enabled", True): continue
                    key = metric["id"]
                    with STATE_LOCK:
                        due = time.monotonic() >= next_run.get(key, 0)
                        if due and key not in self.metric_running:
                            self.metric_running.add(key)
                            generation = self.config_generation
                        else:
                            generation = None
                    if generation is not None:
                        self.monitor_pool.submit(self._collect_metric, copy.deepcopy(metric), generation)
                        next_run[key] = time.monotonic() + float(metric.get("interval", 2))
                self.monitor_stop.wait(0.2)
        self.monitor_thread = threading.Thread(target=loop, daemon=True)
        self.monitor_thread.start()

    def public_metrics(self) -> List[Dict[str, Any]]:
        now = now_ms()
        specs = {item["id"]: item for item in self.config.get("monitoring", {}).get("metrics", [])}
        with STATE_LOCK:
            values = copy.deepcopy(list(self.metrics.values()))
        for value in values:
            spec = specs.get(value["id"], {})
            stale_after = float(spec.get("staleAfter", max(float(spec.get("interval", 2)) * 3,
                                                           float(spec.get("timeout", 5)) * 2)))
            value["stale"] = now - value.get("updatedAt", 0) > stale_after * 1000
        return values

    def refresh_metrics(self, group: Optional[str] = None, failed_only: bool = False) -> int:
        count = 0
        for metric in list(self.config.get("monitoring", {}).get("metrics", [])):
            if group and metric.get("group") != group:
                continue
            with STATE_LOCK:
                if failed_only and self.metrics.get(metric["id"], {}).get("ok", True):
                    continue
                if metric["id"] in self.metric_running:
                    continue
                self.metric_running.add(metric["id"])
                generation = self.config_generation
            self.monitor_pool.submit(self._collect_metric, copy.deepcopy(metric), generation)
            count += 1
        return count

    def job_history(self) -> List[Dict[str, Any]]:
        self._prune_jobs()
        with STATE_LOCK:
            return [job.public() | {"lines": []} for job in sorted(
                self.jobs.values(), key=lambda item: item.started_at, reverse=True)]

    def clear_finished_jobs(self) -> int:
        with STATE_LOCK:
            ids = [job_id for job_id, job in self.jobs.items()
                   if job.status not in ("queued", "running", "stopping")]
            for job_id in ids:
                del self.jobs[job_id]
        return len(ids)

    def diagnostics(self) -> Dict[str, Any]:
        return redact({"version": VERSION, "uptimeMs": now_ms() - self.started_at,
                       "profile": self.config.get("project", {}).get("name", self.config_path.name),
                       "configPath": str(self.config_path),
                       "transport": self.config.get("target", {}).get("transport", "ssh"),
                       "monitorPaused": self.monitor_paused, "metricCount": len(self.metrics),
                       "runningMetrics": len(self.metric_running),
                       "activeJobs": sum(j.status in ("queued", "running", "stopping") for j in self.jobs.values()),
                       "terminalSessions": len(self.terminal_sessions),
                       "recentErrors": copy.deepcopy(self.recent_errors)})

    def diagnostic_report(self, fmt: str = "json") -> str:
        payload = {"diagnostics": self.diagnostics(), "metrics": self.public_metrics(),
                   "jobs": self.job_history()[:50]}
        if fmt == "markdown":
            d = payload["diagnostics"]
            rows = ["# Camera Debug Studio 诊断报告", "", f"- 版本：{d['version']}",
                    f"- Profile：{d['profile']}", f"- Transport：{d['transport']}",
                    f"- 指标：{d['metricCount']}", f"- 活动任务：{d['activeJobs']}", "", "## 指标快照"]
            rows += [f"- {m['id']}: {m.get('value')} ({'OK' if m.get('ok') else m.get('errorCode')})"
                     for m in payload["metrics"]]
            return "\n".join(rows) + "\n"
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def _redact_diagnostic(self, value: Any) -> Any:
        """Apply standard key redaction and mask configured secret literals in text."""
        safe = redact(copy.deepcopy(value))
        secrets: List[str] = []

        def collect_secrets(item: Any) -> None:
            if isinstance(item, dict):
                for key, child in item.items():
                    if key in SENSITIVE_KEYS and isinstance(child, str) and child:
                        secrets.append(str(child))
                    else:
                        collect_secrets(child)
            elif isinstance(item, list):
                for child in item:
                    collect_secrets(child)

        collect_secrets(self.config)
        if ACCESS_TOKEN:
            secrets.append(ACCESS_TOKEN)

        def scrub(item: Any) -> Any:
            if isinstance(item, dict):
                return {key: scrub(child) for key, child in item.items()}
            if isinstance(item, list):
                return [scrub(child) for child in item]
            if isinstance(item, str):
                for secret in secrets:
                    if secret:
                        item = item.replace(secret, "[REDACTED]")
            return item
        return scrub(safe)

    def _record_diagnostic_event(self, event_type: str, details: Dict[str, Any]) -> None:
        """Append a bounded, redacted event when a diagnostic session is active."""
        with STATE_LOCK:
            session = self.diagnostic_session
            if not session or session.get("status") != "active":
                return
            event = self._redact_diagnostic({"time": now_ms(), "type": event_type,
                                             "details": details})
            session["timeline"].append(event)
            if len(session["timeline"]) > MAX_DIAGNOSTIC_EVENTS:
                del session["timeline"][:-MAX_DIAGNOSTIC_EVENTS]

    def start_diagnostic_session(self, name: str = "") -> Dict[str, Any]:
        clean_name = str(name or "诊断会话").strip()
        if not clean_name:
            clean_name = "诊断会话"
        if len(clean_name) > 100:
            raise ApiError("诊断会话名称不能超过 100 个字符", "invalid_session_name")
        started = now_ms()
        snapshot = self.public_metrics()
        with STATE_LOCK:
            if self.diagnostic_session and self.diagnostic_session.get("status") == "active":
                raise ApiError("已有活动诊断会话", "diagnostic_session_active")
            self.diagnostic_session = {
                "id": uuid.uuid4().hex[:12], "name": clean_name,
                "startedAt": started, "endedAt": None, "status": "active",
                "metricSnapshot": self._redact_diagnostic(snapshot), "timeline": [],
            }
        self._record_diagnostic_event("session_started", {
            "metricCount": len(snapshot),
            "profile": self.config.get("project", {}).get("name", self.config_path.name),
            "transport": self.config.get("target", {}).get("transport", "ssh"),
        })
        return self.get_diagnostic_session()

    def get_diagnostic_session(self) -> Optional[Dict[str, Any]]:
        with STATE_LOCK:
            if self.diagnostic_session is None:
                return None
            return self._redact_diagnostic(self.diagnostic_session)

    def end_diagnostic_session(self) -> Dict[str, Any]:
        self._record_diagnostic_event("session_ended", {})
        with STATE_LOCK:
            if not self.diagnostic_session or self.diagnostic_session.get("status") != "active":
                raise ApiError("没有活动诊断会话", "diagnostic_session_not_active")
            self.diagnostic_session["endedAt"] = now_ms()
            self.diagnostic_session["status"] = "ended"
        session = self.get_diagnostic_session()
        assert session is not None
        return session

    def clear_diagnostic_session(self) -> bool:
        with STATE_LOCK:
            if self.diagnostic_session and self.diagnostic_session.get("status") == "active":
                raise ApiError("请先结束活动诊断会话", "diagnostic_session_active")
            existed = self.diagnostic_session is not None
            self.diagnostic_session = None
        return existed

    def diagnostic_session_report(self, fmt: str = "json") -> str:
        session = self.get_diagnostic_session()
        if session is None:
            raise ApiError("诊断会话不存在", "diagnostic_session_not_found")
        payload = self._redact_diagnostic({"session": session})
        if fmt == "json":
            return json.dumps(payload, ensure_ascii=False, indent=2)
        if fmt != "markdown":
            raise ApiError("导出格式只能是 json 或 markdown", "invalid_report_format")
        rows = ["# Camera Debug Studio 诊断会话", "",
                f"- ID：{session['id']}", f"- 名称：{session['name']}",
                f"- 状态：{session['status']}", f"- 开始时间：{session['startedAt']}",
                f"- 结束时间：{session.get('endedAt') or '-'}", "", "## 初始指标快照"]
        rows += [f"- {item.get('id')}: {item.get('value')} "
                 f"({'OK' if item.get('ok') else item.get('errorCode', 'ERROR')})"
                 for item in session.get("metricSnapshot", [])]
        rows += ["", "## 时间线"]
        rows += [f"- {item['time']} · {item['type']} · "
                 f"{json.dumps(item.get('details', {}), ensure_ascii=False)}"
                 for item in session.get("timeline", [])]
        return "\n".join(rows) + "\n"


RUNTIME: Runtime
ACCESS_TOKEN = ""


class Handler(BaseHTTPRequestHandler):
    server_version = f"CameraDebugStudio/{VERSION}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def json_response(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def error_response(self, message: str, status: int = 400, code: str = "bad_request",
                       details: Any = None) -> None:
        self.json_response({"error": {"code": code, "message": str(message), "details": details}}, status)

    def authorized(self, parsed: Any = None) -> bool:
        parsed = parsed or urlparse(self.path)
        return token_authorized(ACCESS_TOKEN, self.headers.get("X-Access-Token", ""), parsed.query)

    def body_json(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ApiError("Content-Length 无效", "invalid_content_length")
        if length < 0 or length > MAX_BODY_BYTES:
            raise ApiError("请求体过大", "request_too_large")
        return json.loads(self.rfile.read(length) or b"{}")

    def websocket_send(self, payload: Dict[str, Any], opcode: int = 1) -> None:
        self.connection.sendall(websocket_frame(payload, opcode))

    def websocket_read(self) -> Optional[Dict[str, Any]]:
        message = read_websocket_message(self.rfile, MAX_WS_BYTES)
        if message and message.get("_control") == "ping":
            payload = message["payload"]
            self.connection.sendall(b"\x8a" + bytes([len(payload)]) + payload)
            return {}
        return message

    def terminal_websocket(self) -> None:
        if not self.authorized():
            return self.error_response("访问令牌无效", HTTPStatus.UNAUTHORIZED, "unauthorized")
        key = self.headers.get("Sec-WebSocket-Key", "")
        if not key or self.headers.get("Upgrade", "").lower() != "websocket":
            return self.json_response({"error": "需要 WebSocket Upgrade"}, 426)
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        send_lock = threading.Lock()
        def send(payload: Dict[str, Any]) -> None:
            with send_lock:
                self.websocket_send(payload)
        session: Optional[TerminalSession] = None
        try:
            session = TerminalSession(RUNTIME, send)
            with STATE_LOCK:
                RUNTIME.terminal_sessions.add(session)
            send({"type": "ready", "transport": RUNTIME.config.get("target", {}).get("transport", "ssh")})
            while not session.closed.is_set():
                message = self.websocket_read()
                if message is None:
                    break
                if message.get("type") == "input":
                    session.input(str(message.get("data", "")))
                elif message.get("type") == "resize":
                    session.resize(int(message.get("columns", 100)), int(message.get("rows", 30)))
        except Exception as exc:
            try:
                send({"type": "error", "message": str(exc)})
            except OSError:
                pass
        finally:
            if session:
                session.close()
                with STATE_LOCK:
                    RUNTIME.terminal_sessions.discard(session)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith(("/api/", "/ws/")) and not self.authorized(parsed):
            return self.error_response("访问令牌无效", HTTPStatus.UNAUTHORIZED, "unauthorized")
        if path == "/ws/terminal":
            return self.terminal_websocket()
        if path == "/api/config":
            return self.json_response(RUNTIME.safe_config())
        if path == "/api/metrics":
            return self.json_response({"metrics": RUNTIME.public_metrics(), "paused": RUNTIME.monitor_paused})
        if path == "/api/version":
            return self.json_response({"version": VERSION})
        if path == "/api/diagnostics":
            return self.json_response(RUNTIME.diagnostics())
        if path == "/api/diagnostics/report":
            query = dict(item.split("=", 1) for item in parsed.query.split("&") if "=" in item)
            return self.json_response({"format": query.get("format", "json"),
                                       "content": RUNTIME.diagnostic_report(query.get("format", "json"))})
        if path in ("/api/diagnostic-session", "/api/diagnostics/session"):
            return self.json_response({"session": RUNTIME.get_diagnostic_session()})
        if path in ("/api/diagnostic-session/report", "/api/diagnostics/session/report"):
            query = dict(item.split("=", 1) for item in parsed.query.split("&") if "=" in item)
            fmt = query.get("format", "json")
            try:
                return self.json_response({"format": fmt,
                                           "content": RUNTIME.diagnostic_session_report(fmt)})
            except ApiError as exc:
                return self.error_response(str(exc), HTTPStatus.BAD_REQUEST, exc.code, exc.details)
        if path == "/api/jobs":
            return self.json_response({"jobs": RUNTIME.job_history()})
        if path == "/api/config/profiles":
            return self.json_response({"profiles": RUNTIME.profiles()})
        if path == "/api/pytest/collect":
            try:
                return self.json_response(RUNTIME.collect_pytest())
            except ValueError as exc:
                return self.error_response(str(exc), HTTPStatus.BAD_REQUEST, "pytest_collect_failed")
        if path == "/api/manual":
            manual = ROOT / "docs" / "用户手册.md"
            if not manual.is_file():
                return self.error_response("用户手册不存在", 404, "not_found")
            return self.json_response({"content": manual.read_text(encoding="utf-8")})
        if path.startswith("/api/jobs/"):
            parts = path.strip("/").split("/")
            job = RUNTIME.jobs.get(parts[2]) if len(parts) >= 3 else None
            if not job: return self.error_response("任务不存在", 404, "job_not_found")
            try: after = int(dict(x.split("=", 1) for x in parsed.query.split("&") if "=" in x).get("after", 0))
            except ValueError: after = 0
            return self.json_response(job.public(after))
        return self.serve_static(path)

    def serve_static(self, path: str) -> None:
        relative = "index.html" if path == "/" else path.lstrip("/")
        target = (WEB / relative).resolve()
        if WEB.resolve() not in target.parents or not target.is_file():
            self.send_error(404); return
        mime = {".html": "text/html", ".css": "text/css", ".js": "application/javascript"}.get(target.suffix, "application/octet-stream")
        body = target.read_bytes()
        self.send_response(200); self.send_header("Content-Type", f"{mime}; charset=utf-8")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_POST(self) -> None:
        try:
            if not self.authorized():
                return self.error_response("访问令牌无效", HTTPStatus.UNAUTHORIZED, "unauthorized")
            data = self.body_json()
            if self.path == "/api/commands":
                command = str(data.get("command", "")).strip()
                if not command: raise ValueError("命令不能为空")
                return self.json_response(RUNTIME.start_job(command).public(), 201)
            if self.path in ("/api/diagnostic-session/start", "/api/diagnostics/session/start"):
                return self.json_response({"session": RUNTIME.start_diagnostic_session(
                    str(data.get("name", "")))}, 201)
            if self.path in ("/api/diagnostic-session/end", "/api/diagnostics/session/end"):
                return self.json_response({"session": RUNTIME.end_diagnostic_session()})
            if self.path in ("/api/diagnostic-session/clear", "/api/diagnostics/session/clear"):
                return self.json_response({"cleared": RUNTIME.clear_diagnostic_session()})
            if self.path == "/api/tests":
                return self.json_response(RUNTIME.run_test(str(data.get("testId", "")), data.get("params", {})).public(), 201)
            if self.path == "/api/pytest/run":
                return self.json_response(RUNTIME.start_pytest(str(data.get("nodeId", ""))).public(), 201)
            if self.path == "/api/connection/test":
                mode = RUNTIME.config.get("target", {}).get("transport", "ssh")
                name = "SSH 连接测试" if mode == "ssh" else "本机连接测试"
                job = RUNTIME.start_job("printf '__CAMERA_DEBUG_CONNECTED__\\n'", "connection", name)
                return self.json_response(job.public(), 201)
            if self.path == "/api/config/target":
                return self.json_response({"ok": True, "target": RUNTIME.update_target(data)})
            if self.path == "/api/config/switch":
                RUNTIME.switch_profile(str(data.get("file", "")))
                return self.json_response({"ok": True, "configPath": str(RUNTIME.config_path)})
            if self.path == "/api/config/save":
                RUNTIME.save_config(data.get("config"))
                return self.json_response({"ok": True})
            if self.path == "/api/config/validate":
                preview = RUNTIME.preview_config(data.get("config"))
                return self.json_response({"valid": True, "config": preview})
            if self.path in ("/api/config/profiles/copy", "/api/config/copy"):
                copied = RUNTIME.copy_profile(str(data.get("sourceFile", data.get("source", ""))),
                                              str(data.get("file", data.get("destination", ""))),
                                              str(data.get("name", "")))
                return self.json_response({"ok": True, "file": copied["file"],
                                           "profile": copied}, 201)
            if self.path.endswith("/stop") and self.path.startswith("/api/jobs/"):
                job_id = self.path.strip("/").split("/")[2]
                return self.json_response({"stopped": RUNTIME.stop_job(job_id)})
            if self.path == "/api/config/reload":
                RUNTIME.reload(); return self.json_response({"ok": True})
            if self.path == "/api/metrics/control":
                action = str(data.get("action", ""))
                scheduled = None
                if action == "pause": RUNTIME.monitor_paused = True
                elif action == "resume": RUNTIME.monitor_paused = False
                elif action == "refresh":
                    group = data.get("group")
                    if group is not None and not isinstance(group, str):
                        raise ApiError("监控分组必须是字符串", "invalid_metric_group")
                    scheduled = RUNTIME.refresh_metrics(group or None, bool(data.get("failedOnly")))
                else: raise ApiError("未知监控操作", "invalid_monitor_action")
                return self.json_response({"ok": True, "paused": RUNTIME.monitor_paused,
                                           "scheduled": scheduled})
            if self.path == "/api/jobs/clear":
                return self.json_response({"cleared": RUNTIME.clear_finished_jobs()})
            self.error_response("接口不存在", 404, "not_found")
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            if isinstance(exc, ApiError):
                code = exc.code
            elif isinstance(exc, ConfigValidationError):
                code = "invalid_config"
            else:
                code = "bad_request"
            details = getattr(exc, "details", None)
            RUNTIME.record_error("api", str(exc), code)
            self.error_response(str(exc), HTTPStatus.BAD_REQUEST, code, details)
        except Exception as exc:
            RUNTIME.record_error("api", str(exc))
            self.error_response("服务器内部错误", HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error")


def main() -> None:
    parser = argparse.ArgumentParser(description="Camera Debug Studio")
    parser.add_argument("--config", default=str(PROFILE_DIR / "demo-local"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--access-token", default=os.environ.get("CAMERA_DEBUG_ACCESS_TOKEN", ""),
                        help="保护 HTTP API 和 WebSocket 的可选访问令牌")
    args = parser.parse_args()
    global RUNTIME, ACCESS_TOKEN
    ACCESS_TOKEN = args.access_token
    RUNTIME = Runtime(Path(args.config).expanduser().resolve())
    RUNTIME.start_monitor()
    url = f"http://{args.host}:{args.port}"
    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        if exc.errno in (errno.EADDRINUSE, 48, 98, 10048):
            print(f"Camera Debug Studio 已经在运行: {url}")
            if not args.no_browser:
                webbrowser.open(url)
            return
        raise
    print(f"Camera Debug Studio: {url}\nConfig: {RUNTIME.config_path}")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("WARNING: 服务正在非回环地址监听。请仅在可信网络使用，并建议设置 --access-token。",
              file=sys.stderr)
        if not ACCESS_TOKEN:
            print("WARNING: 当前未启用访问令牌，所有可访问该端口的用户都能执行命令。", file=sys.stderr)
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        RUNTIME.monitor_stop.set()
        RUNTIME.monitor_pool.shutdown(wait=False, cancel_futures=True)
        with STATE_LOCK:
            sessions = list(RUNTIME.terminal_sessions)
        for session in sessions:
            session.close()
        server.server_close()


if __name__ == "__main__":
    main()
