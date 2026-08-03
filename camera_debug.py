#!/usr/bin/env python3
"""Camera Debug Studio - dependency-free local web service."""

from __future__ import annotations

import argparse
import base64
import errno
import hashlib
import json
import locale
import os
import re
import signal
import struct
import subprocess
import sys
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
PROFILE_DIR = CONFIG_DIR / "profiles"
TEST_DIR = ROOT / "test"
MODULE_FILES = {
    "project": "project.json",
    "target": "target.json",
    "variables": "variables.json",
    "monitoring": "monitoring.json",
    "topology": "topology.json",
    "tests": "tests.json",
}
STATE_LOCK = threading.RLock()


def now_ms() -> int:
    return int(time.time() * 1000)


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_profile(path: Path) -> Dict[str, Any]:
    if path.is_file():
        return load_json(path)
    if not path.is_dir():
        raise ValueError(f"平台配置不存在: {path}")
    result: Dict[str, Any] = {}
    defaults: Dict[str, Any] = {"variables": {}, "monitoring": {"metrics": []},
                                "topology": {"nodes": [], "edges": []}, "tests": []}
    for key, filename in MODULE_FILES.items():
        module_path = path / filename
        if module_path.is_file():
            result[key] = load_json(module_path)
        elif key in defaults:
            result[key] = defaults[key]
        else:
            raise ValueError(f"平台配置缺少 {filename}")
    local_target = path / "target.local.json"
    if local_target.is_file():
        override = load_json(local_target)
        if not isinstance(override, dict):
            raise ValueError("target.local.json 必须是 JSON 对象")
        result["target"] = {**result["target"], **override}
    return result


def write_json(path: Path, data: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    temporary.replace(path)


def render(template: str, values: Dict[str, Any]) -> str:
    """Replace {name} placeholders without interpreting shell syntax."""
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise ValueError(f"缺少模板参数: {key}")
        return str(values[key])
    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, template)


def apply_parser_transforms(value: Any, parser: Dict[str, Any]) -> Any:
    """Apply optional transformations after extraction and before mapping."""
    if "bit" not in parser:
        return value
    bit = parser["bit"]
    if isinstance(bit, bool) or not isinstance(bit, int) or not 0 <= bit <= 63:
        raise ValueError("parser.bit must be an integer from 0 to 63")
    if isinstance(value, int):
        numeric_value = value
    elif isinstance(value, float) and value.is_integer():
        numeric_value = int(value)
    else:
        raw_value = str(value).strip()
        try:
            numeric_value = int(raw_value, 0)
        except ValueError:
            # Base 0 rejects decimal strings with leading zeroes; those are
            # still valid decimal register output.
            numeric_value = int(raw_value, 10)
    return (numeric_value >> bit) & 1


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
    argv: Optional[List[str]] = None
    cwd: Optional[str] = None

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


class TerminalSession:
    """One persistent PTY/pipe-backed local or SSH shell attached to a WebSocket."""

    def __init__(self, runtime: "Runtime", send: Any):
        self.runtime = runtime
        self.send = send
        self.closed = threading.Event()
        self.master_fd: Optional[int] = None
        self.encoding = str(runtime.config.get("target", {}).get(
            "terminalEncoding", locale.getpreferredencoding(False) if os.name == "nt" else "utf-8"))
        command = runtime.terminal_command()
        if os.name == "nt":
            self.process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                env=runtime.command_environment(),
            )
            threading.Thread(target=self._pump_pipe, args=(self.process.stdout,), daemon=True).start()
            threading.Thread(target=self._pump_pipe, args=(self.process.stderr,), daemon=True).start()
            threading.Thread(target=self._wait_pipe, daemon=True).start()
        else:
            self.master_fd, slave_fd = os.openpty()
            self.process = subprocess.Popen(
                command, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                start_new_session=True, close_fds=True,
                env=runtime.command_environment(),
            )
            os.close(slave_fd)
            threading.Thread(target=self._pump_pty, daemon=True).start()

    def _emit(self, chunk: bytes) -> None:
        self.send({"type": "output", "data": chunk.decode(self.encoding, "replace")})

    def _pump_pty(self) -> None:
        try:
            while not self.closed.is_set():
                chunk = os.read(self.master_fd, 4096)  # type: ignore[arg-type]
                if not chunk:
                    break
                self._emit(chunk)
        except OSError:
            pass
        finally:
            code = self.process.poll()
            if code is None:
                code = self.process.wait()
            if not self.closed.is_set():
                try:
                    self.send({"type": "exit", "code": code})
                except OSError:
                    pass
            self.closed.set()

    def _pump_pipe(self, stream: Any) -> None:
        try:
            while not self.closed.is_set():
                chunk = stream.read1(4096) if hasattr(stream, "read1") else stream.read(4096)
                if not chunk:
                    break
                self._emit(chunk)
        except OSError:
            pass

    def _wait_pipe(self) -> None:
        code = self.process.wait()
        if not self.closed.is_set():
            try:
                self.send({"type": "exit", "code": code})
            except OSError:
                pass
        self.closed.set()

    def input(self, data: str) -> None:
        if self.closed.is_set():
            return
        if os.name == "nt":
            if data == "\x03":
                try:
                    self.process.send_signal(signal.CTRL_BREAK_EVENT)
                except OSError:
                    pass
            elif self.process.stdin:
                if data == "\r":
                    data = "\r\n"
                self.process.stdin.write(data.encode(self.encoding, "replace"))
                self.process.stdin.flush()
        else:
            os.write(self.master_fd, data.encode("utf-8"))  # type: ignore[arg-type]

    def resize(self, columns: int, rows: int) -> None:
        if os.name == "nt" or self.master_fd is None:
            return
        import fcntl
        import termios
        columns = max(20, min(400, columns))
        rows = max(5, min(200, rows))
        fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))

    def close(self) -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        if os.name == "nt":
            try:
                self.process.terminate()
            except OSError:
                pass
        else:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                os.close(self.master_fd)  # type: ignore[arg-type]
            except OSError:
                pass


class Runtime:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config = load_profile(config_path)
        self.jobs: Dict[str, Job] = {}
        self.terminal_sessions: set[TerminalSession] = set()
        self.metrics: Dict[str, Dict[str, Any]] = {}
        self.monitor_stop = threading.Event()
        self.monitor_thread: Optional[threading.Thread] = None

    def reload(self) -> None:
        self.config = load_profile(self.config_path)

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
            parser = metric.get("parser", {})
            if "bit" in parser:
                bit = parser["bit"]
                if isinstance(bit, bool) or not isinstance(bit, int) or not 0 <= bit <= 63:
                    raise ValueError(f"监控指标 {metric['id']} 的 parser.bit 必须是 0 到 63 的整数")
            ids.add(metric["id"])
        topology = data.get("topology")
        if topology is not None:
            if not isinstance(topology, dict) or not isinstance(topology.get("nodes"), list) or not isinstance(topology.get("edges"), list):
                raise ValueError("topology.nodes 和 topology.edges 必须是数组")
            node_ids = {node.get("id") for node in topology["nodes"] if isinstance(node, dict)}
            if None in node_ids or len(node_ids) != len(topology["nodes"]):
                raise ValueError("拓扑节点必须包含不重复的 id")
            if any(not re.fullmatch(r"[A-Za-z0-9_-]+", str(node_id)) for node_id in node_ids):
                raise ValueError("拓扑节点 id 只能包含字母、数字、下划线和连字符")
            for node in topology["nodes"]:
                try:
                    x, y = float(node.get("x")), float(node.get("y"))
                except (TypeError, ValueError):
                    raise ValueError("拓扑节点必须包含数值坐标 x 和 y")
                if not 0 <= x <= 100 or not 0 <= y <= 100:
                    raise ValueError("拓扑节点坐标必须在 0 到 100 之间")
            for edge in topology["edges"]:
                if not isinstance(edge, dict) or edge.get("from") not in node_ids or edge.get("to") not in node_ids:
                    raise ValueError("拓扑连接引用了不存在的节点")

    def save_config(self, data: Dict[str, Any]) -> None:
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
        self.config = data
        self.metrics.clear()

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
            target["password"] = str(values.get("password", ""))
            target["sshOptions"] = ["StrictHostKeyChecking=accept-new"]
        else:
            target["localShell"] = str(values.get("localShell", target.get("localShell", "/bin/sh")))
        self.config["target"] = target
        if self.config_path.is_dir():
            self.write_target(target)
        else:
            write_json(self.config_path, self.config)
        self.metrics.clear()
        return target

    def command_environment(self) -> Dict[str, str]:
        environment = dict(os.environ)
        password = str(self.config.get("target", {}).get("password", ""))
        if self.config.get("target", {}).get("transport") == "ssh" and password:
            askpass_script = str(ROOT / "ssh_askpass.py")
            # Windows OpenSSH uses CreateProcess for SSH_ASKPASS and cannot
            # execute a .py file directly. Include the running interpreter so
            # password authentication works regardless of file associations.
            askpass = (subprocess.list2cmdline([sys.executable, askpass_script])
                       if os.name == "nt" else askpass_script)
            environment.update({
                "SSH_ASKPASS": askpass,
                "SSH_ASKPASS_REQUIRE": "force",
                "CAMERA_DEBUG_SSH_PASSWORD": password,
                "DISPLAY": environment.get("DISPLAY", "camera-debug:0"),
            })
        return environment

    def transport_command(self, remote_command: str) -> List[str]:
        target = self.config.get("target", {})
        mode = target.get("transport", "ssh")
        if mode == "local":
            shell = target.get("localShell")
            if os.name == "nt":
                if not shell or str(shell).replace("\\", "/").endswith(("/sh", "/bash", "/zsh")):
                    shell = os.environ.get("COMSPEC", "cmd.exe")
                name = str(shell).replace("\\", "/").rsplit("/", 1)[-1].lower()
                if name in ("powershell", "powershell.exe", "pwsh", "pwsh.exe"):
                    return [str(shell), "-NoLogo", "-NoProfile", "-Command", remote_command]
                return [str(shell), "/d", "/s", "/c", remote_command]
            if shell:
                return [shell, "-lc", remote_command]
            return ["sh", "-lc", remote_command]
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
        if target.get("password"):
            argv += ["-o", "BatchMode=no", "-o", "NumberOfPasswordPrompts=1"]
        else:
            argv += ["-o", "BatchMode=yes"]
        return argv + [destination, remote_command]

    def terminal_command(self) -> List[str]:
        """Build a persistent interactive shell command for a PTY session."""
        target = self.config.get("target", {})
        mode = target.get("transport", "ssh")
        if mode == "local":
            if os.name == "nt":
                shell = target.get("localShell")
                if not shell or str(shell).replace("\\", "/").endswith(("/sh", "/bash", "/zsh")):
                    shell = os.environ.get("COMSPEC", "cmd.exe")
                name = str(shell).replace("\\", "/").rsplit("/", 1)[-1].lower()
                return [str(shell), "-NoLogo"] if name in ("powershell", "powershell.exe", "pwsh", "pwsh.exe") else [str(shell), "/Q"]
            shell = target.get("localShell") or os.environ.get("SHELL", "/bin/sh")
            return [shell, "-l"]
        if mode != "ssh":
            raise ValueError(f"不支持的 transport: {mode}")
        host = target.get("host", "")
        if not host:
            raise ValueError("SSH host 未配置")
        user = target.get("user", "")
        destination = f"{user}@{host}" if user else host
        argv = [target.get("sshBinary", "ssh"), "-tt", "-p", str(target.get("port", 22))]
        argv += ["-o", f"ConnectTimeout={int(target.get('connectTimeout', 8))}"]
        argv += ["-o", "ServerAliveInterval=15"]
        if target.get("identityFile"):
            argv += ["-i", os.path.expanduser(target["identityFile"])]
        for option in target.get("sshOptions", []):
            argv += ["-o", str(option)]
        if target.get("password"):
            argv += ["-o", "BatchMode=no", "-o", "NumberOfPasswordPrompts=1"]
        else:
            argv += ["-o", "BatchMode=yes"]
        return argv + [destination]

    def start_job(self, command: str, kind: str = "command", name: str = "命令") -> Job:
        job = Job(uuid.uuid4().hex[:12], kind, name, command)
        with STATE_LOCK:
            self.jobs[job.id] = job
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
        job = Job(uuid.uuid4().hex[:12], "pytest", f"Pytest · {node_id}",
                  " ".join(argv), argv=argv, cwd=str(ROOT))
        with STATE_LOCK:
            self.jobs[job.id] = job
        threading.Thread(target=self._run_job, args=(job,), daemon=True).start()
        return job

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
        try:
            argv = job.argv or self.transport_command(job.command)
            flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            process = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1, errors="replace", creationflags=flags,
                start_new_session=(os.name != "nt"), env=self.command_environment(),
                cwd=job.cwd,
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
            value = apply_parser_transforms(value, parser)
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

    def body_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def websocket_send(self, payload: Dict[str, Any], opcode: int = 1) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        length = len(body)
        header = bytes([0x80 | opcode])
        if length < 126:
            header += bytes([length])
        elif length < 65536:
            header += bytes([126]) + struct.pack("!H", length)
        else:
            header += bytes([127]) + struct.pack("!Q", length)
        self.connection.sendall(header + body)

    def websocket_read(self) -> Optional[Dict[str, Any]]:
        first = self.rfile.read(2)
        if len(first) != 2:
            return None
        opcode, length = first[0] & 0x0F, first[1] & 0x7F
        masked = bool(first[1] & 0x80)
        if length == 126:
            length = struct.unpack("!H", self.rfile.read(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self.rfile.read(8))[0]
        if length > 1024 * 1024:
            raise ValueError("WebSocket 消息过大")
        mask = self.rfile.read(4) if masked else b""
        payload = self.rfile.read(length)
        if masked:
            payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        if opcode == 8:
            return None
        if opcode == 9:
            self.connection.sendall(b"\x8a" + bytes([len(payload)]) + payload)
            return {}
        if opcode != 1:
            return {}
        return json.loads(payload.decode("utf-8"))

    def terminal_websocket(self) -> None:
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
        if path == "/ws/terminal":
            return self.terminal_websocket()
        if path == "/api/config":
            safe = dict(RUNTIME.config)
            safe["configPath"] = str(RUNTIME.config_path)
            return self.json_response(safe)
        if path == "/api/metrics":
            return self.json_response({"metrics": list(RUNTIME.metrics.values())})
        if path == "/api/config/profiles":
            return self.json_response({"profiles": RUNTIME.profiles()})
        if path == "/api/pytest/collect":
            try:
                return self.json_response(RUNTIME.collect_pytest())
            except ValueError as exc:
                return self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        if path == "/api/manual":
            manual = ROOT / "docs" / "用户手册.md"
            if not manual.is_file():
                return self.json_response({"error": "用户手册不存在"}, 404)
            return self.json_response({"content": manual.read_text(encoding="utf-8")})
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
    parser.add_argument("--config", default=str(PROFILE_DIR / "demo-local"))
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
        RUNTIME.monitor_stop.set()
        with STATE_LOCK:
            sessions = list(RUNTIME.terminal_sessions)
        for session in sessions:
            session.close()
        server.server_close()


if __name__ == "__main__":
    main()
