#!/usr/bin/env python3
"""Camera Debug Studio - dependency-free local web service."""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import signal
import subprocess
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
CONFIG_DIR = ROOT / "configs"
STATE_LOCK = threading.RLock()


def now_ms() -> int:
    return int(time.time() * 1000)


def load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def render(template: str, values: Dict[str, Any]) -> str:
    """Replace {name} placeholders without interpreting shell syntax."""
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise ValueError(f"缺少模板参数: {key}")
        return str(values[key])
    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, template)


@dataclass
class Job:
    id: str
    kind: str
    name: str
    command: str
    status: str = "running"
    started_at: int = field(default_factory=now_ms)
    ended_at: Optional[int] = None
    exit_code: Optional[int] = None
    lines: List[Dict[str, Any]] = field(default_factory=list)
    process: Optional[subprocess.Popen] = None

    def append(self, stream: str, text: str) -> None:
        with STATE_LOCK:
            self.lines.append({"time": now_ms(), "stream": stream, "text": text})
            if len(self.lines) > 5000:
                del self.lines[:1000]

    def public(self, after: int = 0) -> Dict[str, Any]:
        with STATE_LOCK:
            return {
                "id": self.id, "kind": self.kind, "name": self.name,
                "command": self.command, "status": self.status,
                "startedAt": self.started_at, "endedAt": self.ended_at,
                "exitCode": self.exit_code, "cursor": len(self.lines),
                "lines": self.lines[after:],
            }


class Runtime:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config = load_json(config_path)
        self.target_password = ""
        self.jobs: Dict[str, Job] = {}
        self.metrics: Dict[str, Dict[str, Any]] = {}
        self.monitor_stop = threading.Event()
        self.monitor_thread: Optional[threading.Thread] = None

    def reload(self) -> None:
        self.config = load_json(self.config_path)

    def profiles(self) -> List[Dict[str, Any]]:
        result = []
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
        if Path(filename).name != filename or not filename.endswith(".json"):
            raise ValueError("配置文件名无效")
        target_path = (CONFIG_DIR / filename).resolve()
        if CONFIG_DIR.resolve() not in target_path.parents or not target_path.is_file():
            raise ValueError("配置文件不存在")
        data = load_json(target_path)
        self.validate_config(data)
        self.config_path = target_path
        self.config = data
        self.metrics.clear()

    @staticmethod
    def validate_config(data: Dict[str, Any]) -> None:
        if not isinstance(data, dict):
            raise ValueError("配置根节点必须是 JSON 对象")
        if not isinstance(data.get("target"), dict):
            raise ValueError("缺少 target 配置")
        monitoring = data.get("monitoring", {})
        if not isinstance(monitoring, dict) or not isinstance(monitoring.get("metrics", []), list):
            raise ValueError("monitoring.metrics 必须是数组")
        if not isinstance(data.get("tests", []), list):
            raise ValueError("tests 必须是数组")
        ids = set()
        for metric in monitoring.get("metrics", []):
            if not isinstance(metric, dict) or not metric.get("id") or not metric.get("command"):
                raise ValueError("每个监控指标必须包含 id 和 command")
            if metric["id"] in ids:
                raise ValueError(f"监控指标 id 重复: {metric['id']}")
            ids.add(metric["id"])

    def save_config(self, data: Dict[str, Any]) -> None:
        self.validate_config(data)
        temporary = self.config_path.with_suffix(self.config_path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        temporary.replace(self.config_path)
        self.config = data
        self.metrics.clear()

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
            password = str(values.get("password", ""))
            if password:
                self.target_password = password
            elif values.get("clearPassword"):
                self.target_password = ""
            target["sshOptions"] = ["StrictHostKeyChecking=accept-new"]
        else:
            target["localShell"] = str(values.get("localShell", target.get("localShell", "/bin/sh")))
        self.config["target"] = target
        temporary = self.config_path.with_suffix(self.config_path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(self.config, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        temporary.replace(self.config_path)
        self.metrics.clear()
        return target

    def command_environment(self) -> Dict[str, str]:
        environment = dict(os.environ)
        if self.config.get("target", {}).get("transport") == "ssh" and self.target_password:
            environment.update({
                "SSH_ASKPASS": str(ROOT / "ssh_askpass.py"),
                "SSH_ASKPASS_REQUIRE": "force",
                "CAMERA_DEBUG_SSH_PASSWORD": self.target_password,
                "DISPLAY": environment.get("DISPLAY", "camera-debug:0"),
            })
        return environment

    def transport_command(self, remote_command: str) -> List[str]:
        target = self.config.get("target", {})
        mode = target.get("transport", "ssh")
        if mode == "local":
            shell = target.get("localShell")
            if shell:
                return [shell, "-lc", remote_command]
            return ["cmd", "/c", remote_command] if os.name == "nt" else ["sh", "-lc", remote_command]
        if mode != "ssh":
            raise ValueError(f"不支持的 transport: {mode}")
        host = target.get("host", "")
        if not host:
            raise ValueError("SSH host 未配置")
        user = target.get("user", "")
        destination = f"{user}@{host}" if user else host
        argv = [target.get("sshBinary", "ssh"), "-p", str(target.get("port", 22))]
        argv += ["-o", f"ConnectTimeout={int(target.get('connectTimeout', 8))}"]
        argv += ["-o", "ServerAliveInterval=15"]
        if target.get("identityFile"):
            argv += ["-i", os.path.expanduser(target["identityFile"])]
        for option in target.get("sshOptions", []):
            argv += ["-o", str(option)]
        if self.target_password:
            argv += ["-o", "BatchMode=no", "-o", "NumberOfPasswordPrompts=1"]
        else:
            argv += ["-o", "BatchMode=yes"]
        return argv + [destination, remote_command]

    def start_job(self, command: str, kind: str = "command", name: str = "命令") -> Job:
        job = Job(uuid.uuid4().hex[:12], kind, name, command)
        with STATE_LOCK:
            self.jobs[job.id] = job
        threading.Thread(target=self._run_job, args=(job,), daemon=True).start()
        return job

    def _run_job(self, job: Job) -> None:
        try:
            argv = self.transport_command(job.command)
            flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            process = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1, errors="replace", creationflags=flags,
                start_new_session=(os.name != "nt"), env=self.command_environment(),
            )
            job.process = process
            def pump(stream: Any, label: str) -> None:
                for line in iter(stream.readline, ""):
                    job.append(label, line.rstrip("\r\n"))
                stream.close()
            threads = [
                threading.Thread(target=pump, args=(process.stdout, "stdout"), daemon=True),
                threading.Thread(target=pump, args=(process.stderr, "stderr"), daemon=True),
            ]
            for thread in threads: thread.start()
            code = process.wait()
            for thread in threads: thread.join(timeout=1)
            job.exit_code = code
            job.status = "success" if code == 0 else ("stopped" if job.status == "stopping" else "failed")
        except Exception as exc:
            job.append("stderr", str(exc))
            job.status = "failed"
            job.exit_code = -1
        finally:
            job.ended_at = now_ms()
            job.process = None

    def stop_job(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if not job or not job.process or job.status != "running":
            return False
        job.status = "stopping"
        try:
            if os.name == "nt":
                job.process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(job.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
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
        return self.start_job(command, "test", test.get("name", test_id))

    def query_metric(self, metric: Dict[str, Any]) -> Dict[str, Any]:
        command = render(metric["command"], self.config.get("variables", {}))
        try:
            result = subprocess.run(self.transport_command(command), capture_output=True, text=True,
                                    timeout=float(metric.get("timeout", 5)), errors="replace",
                                    env=self.command_environment())
            output = (result.stdout + "\n" + result.stderr).strip()
            parser = metric.get("parser", {"type": "text"})
            parser_type = parser.get("type", "text")
            if parser_type == "regex":
                match = re.search(parser["pattern"], output, re.MULTILINE)
                if not match:
                    raise ValueError("输出不匹配正则")
                value: Any = match.group(int(parser.get("group", 1)))
            elif parser_type == "number":
                match = re.search(parser.get("pattern", r"[-+]?\d+(?:\.\d+)?"), output)
                if not match:
                    raise ValueError("未找到数值")
                value = float(match.group(0))
            else:
                value = output
            mapping = parser.get("map", {})
            value = mapping.get(str(value), value)
            return {"id": metric["id"], "value": value, "ok": result.returncode == 0,
                    "raw": output, "updatedAt": now_ms()}
        except Exception as exc:
            return {"id": metric["id"], "value": "--", "ok": False,
                    "error": str(exc), "updatedAt": now_ms()}

    def start_monitor(self) -> None:
        if self.monitor_thread and self.monitor_thread.is_alive():
            return
        self.monitor_stop.clear()
        def loop() -> None:
            next_run: Dict[str, float] = {}
            while not self.monitor_stop.is_set():
                for metric in self.config.get("monitoring", {}).get("metrics", []):
                    if not metric.get("enabled", True): continue
                    key = metric["id"]
                    if time.monotonic() >= next_run.get(key, 0):
                        self.metrics[key] = self.query_metric(metric)
                        next_run[key] = time.monotonic() + float(metric.get("interval", 2))
                self.monitor_stop.wait(0.2)
        self.monitor_thread = threading.Thread(target=loop, daemon=True)
        self.monitor_thread.start()


RUNTIME: Runtime


class Handler(BaseHTTPRequestHandler):
    server_version = "CameraDebugStudio/0.1"

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

    def body_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/config":
            safe = dict(RUNTIME.config)
            safe["configPath"] = str(RUNTIME.config_path)
            return self.json_response(safe)
        if path == "/api/metrics":
            return self.json_response({"metrics": list(RUNTIME.metrics.values())})
        if path == "/api/config/profiles":
            return self.json_response({"profiles": RUNTIME.profiles()})
        if path.startswith("/api/jobs/"):
            parts = path.strip("/").split("/")
            job = RUNTIME.jobs.get(parts[2]) if len(parts) >= 3 else None
            if not job: return self.json_response({"error": "任务不存在"}, 404)
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
            data = self.body_json()
            if self.path == "/api/commands":
                command = str(data.get("command", "")).strip()
                if not command: raise ValueError("命令不能为空")
                return self.json_response(RUNTIME.start_job(command).public(), 201)
            if self.path == "/api/tests":
                return self.json_response(RUNTIME.run_test(str(data.get("testId", "")), data.get("params", {})).public(), 201)
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
            if self.path.endswith("/stop") and self.path.startswith("/api/jobs/"):
                job_id = self.path.strip("/").split("/")[2]
                return self.json_response({"stopped": RUNTIME.stop_job(job_id)})
            if self.path == "/api/config/reload":
                RUNTIME.reload(); return self.json_response({"ok": True})
            self.json_response({"error": "接口不存在"}, 404)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)


def main() -> None:
    parser = argparse.ArgumentParser(description="Camera Debug Studio")
    parser.add_argument("--config", default=str(CONFIG_DIR / "demo-local.json"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    global RUNTIME
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
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        RUNTIME.monitor_stop.set(); server.server_close()


if __name__ == "__main__":
    main()
