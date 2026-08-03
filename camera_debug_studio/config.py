"""Profile persistence, validation helpers, and credential redaction."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict


MODULE_FILES = {
    "project": "project.json",
    "target": "target.json",
    "variables": "variables.json",
    "monitoring": "monitoring.json",
    "topology": "topology.json",
    "tests": "tests.json",
}
SENSITIVE_KEYS = {"password", "identityFileContent", "privateKey", "token", "accessToken"}


class ConfigValidationError(ValueError):
    """A validation error that can be mapped back to an editor field."""

    def __init__(self, message: str, path: str = ""):
        super().__init__(message)
        self.path = path
        self.details = {"path": path} if path else {}


def _invalid(message: str, path: str = "") -> None:
    raise ConfigValidationError(message, path)


def redact(value: Any) -> Any:
    """Return a deep, JSON-safe copy with credentials removed."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in SENSITIVE_KEYS:
                if key == "password":
                    result["passwordConfigured"] = bool(item)
                continue
            result[key] = redact(item)
        return result
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_profile(path: Path) -> Dict[str, Any]:
    if path.is_file():
        return load_json(path)
    if not path.is_dir():
        raise ValueError(f"平台配置不存在: {path}")
    result: Dict[str, Any] = {}
    defaults: Dict[str, Any] = {
        "variables": {},
        "monitoring": {"metrics": []},
        "topology": {"nodes": [], "edges": []},
        "tests": [],
    }
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
    """Atomically replace JSON and retain the previous version as ``.bak``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        backup.write_bytes(path.read_bytes())
    temporary.replace(path)


def render(template: str, values: Dict[str, Any]) -> str:
    """Replace ``{name}`` placeholders without interpreting shell syntax."""
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise ValueError(f"缺少模板参数: {key}")
        return str(values[key])
    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, template)


def validate_config(data: Dict[str, Any]) -> None:
    if not isinstance(data, dict):
        _invalid("配置根节点必须是 JSON 对象", "$")
    if not isinstance(data.get("target"), dict):
        _invalid("缺少 target 配置", "target")
    monitoring = data.get("monitoring", {})
    if not isinstance(monitoring, dict) or not isinstance(monitoring.get("metrics", []), list):
        _invalid("monitoring.metrics 必须是数组", "monitoring.metrics")
    if not isinstance(data.get("tests", []), list):
        _invalid("tests 必须是数组", "tests")
    ids = set()
    for metric_index, metric in enumerate(monitoring.get("metrics", [])):
        metric_path = f"monitoring.metrics[{metric_index}]"
        if not isinstance(metric, dict) or not metric.get("id") or not metric.get("command"):
            _invalid("每个监控指标必须包含 id 和 command", metric_path)
        if metric["id"] in ids:
            _invalid(f"监控指标 id 重复: {metric['id']}", f"{metric_path}.id")
        ids.add(metric["id"])
        if not re.fullmatch(r"[A-Za-z0-9_-]+", str(metric["id"])):
            _invalid(f"监控指标 id 无效: {metric['id']}", f"{metric_path}.id")
        try:
            interval = float(metric.get("interval", 2))
            timeout = float(metric.get("timeout", 5))
            stale_after = float(metric.get("staleAfter", max(interval * 3, timeout * 2)))
        except (TypeError, ValueError):
            _invalid(f"监控指标 {metric['id']} 的周期或超时无效", metric_path)
        if not 0.1 <= interval <= 86400 or not 0.1 <= timeout <= 3600 or stale_after < interval:
            _invalid(f"监控指标 {metric['id']} 的周期、超时或 staleAfter 超出范围", metric_path)
        parser = metric.get("parser", {"type": "text"})
        if not isinstance(parser, dict) or parser.get("type", "text") not in ("text", "number", "regex"):
            _invalid(f"监控指标 {metric['id']} 的解析器无效", f"{metric_path}.parser")
        if "bit" in parser:
            bit = parser["bit"]
            if isinstance(bit, bool) or not isinstance(bit, int) or not 0 <= bit <= 63:
                _invalid(f"监控指标 {metric['id']} 的 parser.bit 必须是 0 到 63 的整数",
                         f"{metric_path}.parser.bit")
        if parser.get("type") in ("number", "regex"):
            try:
                re.compile(str(parser.get("pattern", r"[-+]?\d+(?:\.\d+)?")))
            except re.error as exc:
                _invalid(f"监控指标 {metric['id']} 的正则无效: {exc}", f"{metric_path}.parser.pattern")
    test_ids = set()
    for test_index, test in enumerate(data.get("tests", [])):
        test_path = f"tests[{test_index}]"
        if not isinstance(test, dict) or not test.get("id") or not test.get("command"):
            _invalid("每个测试项必须包含 id 和 command", test_path)
        if test["id"] in test_ids:
            _invalid(f"测试项 id 重复: {test['id']}", f"{test_path}.id")
        test_ids.add(test["id"])
        try:
            timeout = float(test.get("timeout", 0))
        except (TypeError, ValueError):
            _invalid(f"测试项 {test['id']} 的 timeout 无效", f"{test_path}.timeout")
        if timeout < 0 or timeout > 86400:
            _invalid(f"测试项 {test['id']} 的 timeout 超出范围", f"{test_path}.timeout")
        expected = test.get("expectedExitCodes", [0])
        if not isinstance(expected, list) or not expected or any(not isinstance(code, int) for code in expected):
            _invalid(f"测试项 {test['id']} 的 expectedExitCodes 无效", f"{test_path}.expectedExitCodes")
        for param_index, spec in enumerate(test.get("params", [])):
            param_path = f"{test_path}.params[{param_index}]"
            if not isinstance(spec, dict) or not spec.get("name"):
                _invalid(f"测试项 {test['id']} 包含无效参数", param_path)
            try:
                re.compile(spec.get("pattern", r"[A-Za-z0-9_.:/@+-]*"))
            except re.error as exc:
                _invalid(f"测试项 {test['id']} 参数正则无效: {exc}", f"{param_path}.pattern")
    topology = data.get("topology")
    if topology is None:
        return
    if not isinstance(topology, dict) or not isinstance(topology.get("nodes"), list) or not isinstance(topology.get("edges"), list):
        _invalid("topology.nodes 和 topology.edges 必须是数组", "topology")
    node_ids = {node.get("id") for node in topology["nodes"] if isinstance(node, dict)}
    if None in node_ids or len(node_ids) != len(topology["nodes"]):
        _invalid("拓扑节点必须包含不重复的 id", "topology.nodes")
    if any(not re.fullmatch(r"[A-Za-z0-9_-]+", str(node_id)) for node_id in node_ids):
        _invalid("拓扑节点 id 只能包含字母、数字、下划线和连字符", "topology.nodes")
    for node_index, node in enumerate(topology["nodes"]):
        node_path = f"topology.nodes[{node_index}]"
        try:
            x, y = float(node.get("x")), float(node.get("y"))
        except (TypeError, ValueError):
            _invalid("拓扑节点必须包含数值坐标 x 和 y", node_path)
        if not 0 <= x <= 100 or not 0 <= y <= 100:
            _invalid("拓扑节点坐标必须在 0 到 100 之间", node_path)
    for edge_index, edge in enumerate(topology["edges"]):
        if not isinstance(edge, dict) or edge.get("from") not in node_ids or edge.get("to") not in node_ids:
            _invalid("拓扑连接引用了不存在的节点", f"topology.edges[{edge_index}]")
