"""Compatibility contracts for the backend/frontend modularization.

These tests intentionally exercise public entry points and HTTP assets rather
than importing implementation modules.  They should remain stable while the
internals are split into packages and browser-native ES modules.
"""

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import camera_debug as app


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "camera_debug.py"


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _get(url, timeout=2):
    request = urllib.request.Request(url)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(request, timeout=timeout)


def _wait_for_server(base_url, process):
    deadline = time.monotonic() + 8
    last_error = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=1)
            raise AssertionError(
                f"entrypoint exited before serving HTTP (code={process.returncode})\n"
                f"stdout:\n{stdout}\nstderr:\n{stderr}"
            )
        try:
            with _get(base_url + "/api/version", timeout=0.5) as response:
                return json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError) as exc:
            last_error = exc
            time.sleep(0.05)
    raise AssertionError(f"entrypoint did not become ready: {last_error}")


def test_import_camera_debug_from_outside_project_has_no_startup_side_effect(tmp_path):
    """The compatibility module must remain importable without starting a server."""
    code = (
        "import json, sys; "
        f"sys.path.insert(0, {str(ROOT)!r}); "
        "import camera_debug; "
        "print(json.dumps({'version': camera_debug.VERSION, "
        "'main': callable(camera_debug.main)}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5, check=True,
    )
    assert json.loads(result.stdout) == {"version": app.VERSION, "main": True}
    assert result.stderr == ""


def test_camera_debug_entrypoint_serves_from_outside_project(tmp_path):
    """Keep ``python3 camera_debug.py`` as the supported production launcher."""
    port = _free_port()
    process = subprocess.Popen(
        [sys.executable, str(ENTRYPOINT), "--no-browser", "--host", "127.0.0.1",
         "--port", str(port), "--config", str(app.PROFILE_DIR / "demo-local")],
        cwd=tmp_path, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    try:
        assert _wait_for_server(f"http://127.0.0.1:{port}", process) == {
            "version": app.VERSION
        }
    finally:
        process.terminate()
        try:
            process.communicate(timeout=4)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=2)
    assert process.returncode is not None


def test_exactly_three_bundled_profiles_remain_loadable():
    expected = {"bmc", "demo-local", "qualcomm"}
    actual = {path.name for path in app.PROFILE_DIR.iterdir() if path.is_dir()}
    assert actual == expected
    for name in sorted(expected):
        loaded = app.load_profile(app.PROFILE_DIR / name)
        app.Runtime.validate_config(loaded)
        assert set(app.MODULE_FILES).issubset(loaded)


_HTML_ASSET = re.compile(
    r"<(?:script|link)\b[^>]*(?:src|href)=[\"']([^\"']+)[\"']", re.IGNORECASE
)
_HTML_MODULE = re.compile(
    r"<script\b(?=[^>]*\btype=[\"']module[\"'])[^>]*\bsrc=[\"']([^\"']+)[\"'][^>]*>",
    re.IGNORECASE,
)
_JS_IMPORT = re.compile(
    r"(?:\bimport\s*(?:[^'\"]*?\sfrom\s*)?|\bexport\s+[^'\"]*?\sfrom\s*)"
    r"['\"]([^'\"]+)['\"]"
)


def _resolve_web_reference(parent, reference):
    clean = reference.split("?", 1)[0].split("#", 1)[0]
    if clean.startswith(("http://", "https://", "//", "data:")):
        return None
    if clean.startswith("/"):
        return clean
    parent_dir = Path(parent).parent
    return "/" + (parent_dir / clean).as_posix().lstrip("/")


def test_browser_assets_and_native_module_graph_are_served(api_server):
    """Every local HTML/ES-module reference must resolve through the HTTP server."""
    base_url, _ = api_server  # fixture below yields the URL and process
    with _get(base_url + "/") as response:
        index_html = response.read().decode("utf-8")
    assert _HTML_MODULE.findall(index_html), (
        "index.html must use a browser-native <script type=\"module\"> entrypoint"
    )
    pending = ["/"]
    visited = set()
    javascript_paths = set()
    while pending:
        path = pending.pop()
        if path in visited:
            continue
        visited.add(path)
        with _get(base_url + path) as response:
            body = response.read().decode("utf-8")
            content_type = response.headers.get_content_type()
        assert content_type in {"text/html", "text/css", "application/javascript"}
        if content_type == "text/html":
            references = _HTML_ASSET.findall(body)
        elif content_type == "application/javascript":
            javascript_paths.add(path)
            references = _JS_IMPORT.findall(body)
        else:
            references = []
        for reference in references:
            resolved = _resolve_web_reference(path, reference)
            if resolved is not None:
                pending.append(resolved)
    assert len(javascript_paths) >= 2, (
        "the native module graph must include the entrypoint and at least one extracted module"
    )


import pytest


@pytest.fixture
def api_server(tmp_path):
    """Run the real compatibility entrypoint for static-resource black-box tests."""
    port = _free_port()
    process = subprocess.Popen(
        [sys.executable, str(ENTRYPOINT), "--no-browser", "--host", "127.0.0.1",
         "--port", str(port), "--config", str(app.PROFILE_DIR / "demo-local")],
        cwd=tmp_path, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    base_url = f"http://127.0.0.1:{port}"
    _wait_for_server(base_url, process)
    yield base_url, process
    process.terminate()
    try:
        process.communicate(timeout=4)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=2)
