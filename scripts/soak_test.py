#!/usr/bin/env python3
"""Run a repeatable Local soak test and emit machine-readable evidence."""

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def request_json(url, method="GET", body=None, timeout=5):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    started = time.monotonic()
    with opener.open(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode())
    return payload, round((time.monotonic() - started) * 1000, 2)


def process_stats(pid):
    rss_kb = 0
    threads = 0
    try:
        output = subprocess.check_output(["ps", "-o", "rss=", "-p", str(pid)], text=True,
                                         stderr=subprocess.DEVNULL).strip()
        if output:
            rss_kb = int(output)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    status = Path(f"/proc/{pid}/status")
    if status.is_file():
        for line in status.read_text(errors="replace").splitlines():
            if line.startswith("VmRSS:") and not rss_kb:
                rss_kb = int(line.split()[1])
            elif line.startswith("Threads:"):
                threads = int(line.split()[1])
    elif sys.platform == "darwin":
        try:
            output = subprocess.check_output(["ps", "-M", "-p", str(pid), "-o", "pid="],
                                             text=True, stderr=subprocess.DEVNULL)
            threads = len([line for line in output.splitlines() if line.strip()])
        except (OSError, subprocess.SubprocessError):
            pass
    children = 0
    try:
        result = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True)
        children = len([line for line in result.stdout.splitlines() if line.strip()])
    except OSError:
        pass
    return {"rssKb": rss_kb, "threads": threads, "children": children}


def write_profile(path):
    modules = {
        "project.json": {"name": "Soak Local", "platform": "local"},
        "target.json": {"transport": "local", "localShell": "/bin/sh"},
        "variables.json": {},
        "monitoring.json": {"metrics": [
            {"id": "fast", "name": "Fast", "group": "SOAK", "command": "printf 1",
             "interval": 0.5, "timeout": 1, "parser": {"type": "number"}},
            {"id": "slow", "name": "Slow", "group": "SOAK", "command": "sleep 0.2; printf 2",
             "interval": 1, "timeout": 1, "parser": {"type": "number"}},
        ]},
        "topology.json": {"nodes": [], "edges": []},
        "tests.json": [],
    }
    for filename, value in modules.items():
        (path / filename).write_text(json.dumps(value), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Camera Debug Studio Local 长稳测试")
    parser.add_argument("--duration", type=float, default=8 * 60 * 60, help="运行秒数，默认 8 小时")
    parser.add_argument("--interval", type=float, default=30, help="采样间隔秒数")
    parser.add_argument("--report", default="soak-report.json", help="JSON 报告路径")
    parser.add_argument("--max-rss-growth-mb", type=float, default=100)
    args = parser.parse_args()
    if args.duration <= 0 or args.interval <= 0:
        parser.error("duration 和 interval 必须大于 0")

    port = free_port()
    samples = []
    errors = []
    started_at = int(time.time() * 1000)
    with tempfile.TemporaryDirectory(prefix="camera-debug-soak-") as directory:
        profile = Path(directory)
        write_profile(profile)
        server_log = (profile / "server.log").open("w+", encoding="utf-8")
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "camera_debug.py"), "--no-browser", "--port", str(port),
             "--config", str(profile)], cwd=ROOT, stdout=server_log, stderr=subprocess.STDOUT,
            text=True, start_new_session=(os.name != "nt"))
        base = f"http://127.0.0.1:{port}"
        try:
            deadline = time.monotonic() + 10
            while True:
                try:
                    request_json(base + "/api/version", timeout=0.5)
                    break
                except (OSError, urllib.error.URLError):
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("服务未能在 10 秒内启动")
                    time.sleep(0.05)

            end = time.monotonic() + args.duration
            sequence = 0
            while time.monotonic() < end:
                sequence += 1
                sample = {"time": int(time.time() * 1000), **process_stats(process.pid)}
                try:
                    metrics, sample["metricsLatencyMs"] = request_json(base + "/api/metrics")
                    diagnostics, sample["diagnosticsLatencyMs"] = request_json(base + "/api/diagnostics")
                    sample["metricCount"] = len(metrics.get("metrics", []))
                    sample["runningMetrics"] = diagnostics.get("runningMetrics", 0)
                    sample["activeJobs"] = diagnostics.get("activeJobs", 0)
                    if sequence % 10 == 0:
                        request_json(base + "/api/commands", "POST", {"command": "printf soak"})
                except Exception as exc:  # evidence must retain transient failures
                    errors.append({"time": sample["time"], "message": str(exc)})
                samples.append(sample)
                time.sleep(min(args.interval, max(0, end - time.monotonic())))
        finally:
            if process.poll() is None:
                if os.name == "nt":
                    process.terminate()
                else:
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
            process.wait()
            server_log.flush()
            server_log.seek(0)
            log_tail = server_log.read()[-4000:]
            server_log.close()

    rss_values = [sample["rssKb"] for sample in samples if sample.get("rssKb")]
    rss_growth_kb = (rss_values[-1] - rss_values[0]) if len(rss_values) >= 2 else 0
    passed = not errors and process.returncode in (0, -signal.SIGTERM)
    if rss_growth_kb > args.max_rss_growth_mb * 1024:
        passed = False
        errors.append({"time": int(time.time() * 1000), "message": "RSS 增长超过阈值"})
    report = {
        "schemaVersion": 1, "startedAt": started_at, "endedAt": int(time.time() * 1000),
        "requestedDurationSeconds": args.duration, "sampleIntervalSeconds": args.interval,
        "sampleCount": len(samples), "passed": passed, "rssGrowthKb": rss_growth_kb,
        "maxRssKb": max(rss_values, default=0),
        "maxThreads": max((sample.get("threads", 0) for sample in samples), default=0),
        "maxChildren": max((sample.get("children", 0) for sample in samples), default=0),
        "errors": errors, "samples": samples, "serverExitCode": process.returncode,
        "serverLogTail": log_tail,
    }
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Soak {'PASS' if passed else 'FAIL'}: {len(samples)} samples, report={args.report}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
