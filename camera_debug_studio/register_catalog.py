"""Validated offline register knowledge base and deterministic decoder."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List


class RegisterCatalogError(ValueError):
    pass


def parse_integer(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise RegisterCatalogError(f"{label} 必须是整数")
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip(), 0)
    except ValueError as exc:
        raise RegisterCatalogError(f"{label} 格式无效：{value}") from exc


class RegisterCatalog:
    def __init__(self, root: Path):
        self.devices: Dict[str, Dict[str, Any]] = {}
        for path in sorted(root.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            self._validate(data)
            self.devices[data["device"]["model"].lower()] = data

    @staticmethod
    def _validate(data: Dict[str, Any]) -> None:
        device = data.get("device", {})
        if not device.get("model") or not isinstance(data.get("registers"), list):
            raise RegisterCatalogError("知识库缺少 device.model 或 registers")
        width = parse_integer(device.get("valueWidthBits", 8), "valueWidthBits")
        addresses, ids = set(), set()
        for register in data["registers"]:
            address, register_id = parse_integer(register.get("address"), "address"), register.get("id")
            if not register_id or register_id in ids or address in addresses:
                raise RegisterCatalogError(f"寄存器 ID 或地址重复：{register_id}")
            addresses.add(address); ids.add(register_id)
            occupied = 0
            for field in register.get("fields", []):
                msb, lsb = parse_integer(field["bits"]["msb"], "msb"), parse_integer(field["bits"]["lsb"], "lsb")
                if lsb < 0 or msb < lsb or msb >= width:
                    raise RegisterCatalogError(f"{register_id}.{field.get('id')} bit 范围无效")
                mask = ((1 << (msb - lsb + 1)) - 1) << lsb
                if occupied & mask:
                    raise RegisterCatalogError(f"{register_id}.{field.get('id')} 位域重叠")
                occupied |= mask

    def summaries(self) -> List[Dict[str, Any]]:
        return [{"model": item["device"]["model"], "registerCount": len(item["registers"]),
                 "datasheet": item["device"].get("datasheet", {})} for item in self.devices.values()]

    def decode(self, model: str, selector: Any, raw_value: Any) -> Dict[str, Any]:
        device = self.devices.get(model.strip().lower())
        if not device:
            raise RegisterCatalogError(f"知识库中没有芯片：{model}")
        selector_text = str(selector).strip()
        try:
            selector_address = int(selector_text, 0)
        except ValueError:
            selector_address = None
        register = next((item for item in device["registers"] if
                         item["id"].lower() == selector_text.lower() or
                         (selector_address is not None and parse_integer(item["address"], "address") == selector_address)), None)
        if not register:
            raise RegisterCatalogError(f"{model} 知识库中没有寄存器：{selector}")
        if not register.get("fields"):
            raise RegisterCatalogError(f"{register['id']} 只有地址索引，尚未录入位域详情，暂时不能判断状态")
        width = parse_integer(register.get("widthBits", device["device"].get("valueWidthBits", 8)), "widthBits")
        value = parse_integer(raw_value, "寄存器值")
        if value < 0 or value >= 1 << width:
            raise RegisterCatalogError(f"寄存器值超出 {width}-bit 范围")
        fields = []
        for field in register["fields"]:
            msb, lsb = field["bits"]["msb"], field["bits"]["lsb"]
            field_value = (value >> lsb) & ((1 << (msb - lsb + 1)) - 1)
            meaning = field.get("values", {}).get(str(field_value))
            fields.append({"id": field["id"], "bits": str(msb) if msb == lsb else f"{msb}:{lsb}",
                           "value": field_value, "meaning": meaning.get("meaning", "未定义值") if meaning else "未定义值",
                           "status": meaning.get("status", "unknown") if meaning else "unknown",
                           "description": field.get("description", "")})
        statuses = [item["status"] for item in fields]
        level = "error" if "error" in statuses else "warning" if "warning" in statuses else "unknown" if "unknown" in statuses else "normal"
        summary = {"normal": "状态正常", "warning": "存在需要关注的状态", "error": "检测到异常状态", "unknown": "存在未定义值，无法完整判断"}[level]
        return {"device": device["device"]["model"], "register": {"id": register["id"], "address": register["address"],
                "name": register.get("name", register["id"]), "description": register.get("description", "")},
                "value": {"hex": f"0x{value:0{max(2, width // 4)}X}", "decimal": value, "binary": f"{value:0{width}b}"},
                "status": {"level": level, "summary": summary}, "fields": fields,
                "datasheet": device["device"].get("datasheet", {})}
