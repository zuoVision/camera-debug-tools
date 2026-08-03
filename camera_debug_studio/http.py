"""HTTP authentication and WebSocket wire-protocol helpers."""

from __future__ import annotations

import hmac
import json
import struct
from typing import Any, BinaryIO, Dict, Optional
from urllib.parse import parse_qs


def token_authorized(access_token: str, header_token: str, query: str) -> bool:
    if not access_token:
        return True
    query_token = parse_qs(query, keep_blank_values=True).get("token", [""])[0]
    return hmac.compare_digest(header_token or query_token, access_token)


def websocket_frame(payload: Dict[str, Any], opcode: int = 1) -> bytes:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    length = len(body)
    header = bytes([0x80 | opcode])
    if length < 126:
        header += bytes([length])
    elif length < 65536:
        header += bytes([126]) + struct.pack("!H", length)
    else:
        header += bytes([127]) + struct.pack("!Q", length)
    return header + body


def read_websocket_message(stream: BinaryIO, max_bytes: int) -> Optional[Dict[str, Any]]:
    first = stream.read(2)
    if len(first) != 2:
        return None
    opcode, length = first[0] & 0x0F, first[1] & 0x7F
    masked = bool(first[1] & 0x80)
    if length == 126:
        length = struct.unpack("!H", stream.read(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", stream.read(8))[0]
    if length > max_bytes:
        raise ValueError("WebSocket 消息过大")
    mask = stream.read(4) if masked else b""
    payload = stream.read(length)
    if len(payload) != length:
        return None
    if masked:
        payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    if opcode == 8:
        return None
    if opcode == 9:
        return {"_control": "ping", "payload": payload}
    if opcode != 1:
        return {}
    return json.loads(payload.decode("utf-8"))
