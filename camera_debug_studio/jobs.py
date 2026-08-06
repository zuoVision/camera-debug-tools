"""Task state and bounded log representation."""

from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


STATE_LOCK = threading.RLock()
MAX_JOB_LINES = 5000


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class Job:
    id: str
    kind: str
    name: str
    command: str
    status: str = "queued"
    started_at: int = field(default_factory=now_ms)
    ended_at: Optional[int] = None
    exit_code: Optional[int] = None
    lines: List[Dict[str, Any]] = field(default_factory=list)
    process: Optional[subprocess.Popen] = None
    argv: Optional[List[str]] = None
    pre_argv: List[List[str]] = field(default_factory=list)
    cwd: Optional[str] = None
    encoding: Optional[str] = None
    timeout: Optional[float] = None
    expected_exit_codes: List[int] = field(default_factory=lambda: [0])
    stop_reason: Optional[str] = None

    def append(self, stream: str, text: str) -> None:
        with STATE_LOCK:
            self.lines.append({"time": now_ms(), "stream": stream, "text": text})
            if len(self.lines) > MAX_JOB_LINES:
                del self.lines[:1000]

    def public(self, after: int = 0) -> Dict[str, Any]:
        with STATE_LOCK:
            return {
                "id": self.id, "kind": self.kind, "name": self.name,
                "command": self.command, "status": self.status,
                "startedAt": self.started_at, "endedAt": self.ended_at,
                "createdAt": self.started_at,
                "durationMs": ((self.ended_at or now_ms()) - self.started_at),
                "exitCode": self.exit_code, "cursor": len(self.lines),
                "stopReason": self.stop_reason,
                "lines": self.lines[after:],
            }
