"""Metric output parsing kept independent from scheduling and transport."""

from __future__ import annotations

import re
from typing import Any, Dict


def apply_parser_transforms(value: Any, parser: Dict[str, Any]) -> Any:
    """Apply optional register transformations before result mapping."""
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
            numeric_value = int(raw_value, 10)
    return (numeric_value >> bit) & 1


def parse_metric_output(parser: Dict[str, Any], output: str) -> Any:
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
    return parser.get("map", {}).get(str(value), value)
