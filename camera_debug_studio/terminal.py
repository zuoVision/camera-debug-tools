"""Persistent local/SSH terminal session lifecycle."""

from __future__ import annotations

import locale
import os
import signal
import struct
import subprocess
import threading
from typing import Any, Optional, Protocol


class TerminalRuntime(Protocol):
    config: dict

    def terminal_command(self) -> list[str]: ...
    def command_environment(self) -> dict[str, str]: ...


class TerminalSession:
    """One persistent PTY/pipe-backed local or SSH shell attached to a WebSocket."""

    def __init__(self, runtime: TerminalRuntime, send: Any):
        self.runtime = runtime
        self.send = send
        self.closed = threading.Event()
        self.master_fd: Optional[int] = None
        target = runtime.config.get("target", {})
        default_encoding = "utf-8" if target.get("transport", "ssh") == "ssh" else locale.getpreferredencoding(False)
        self.encoding = str(target.get("terminalEncoding", default_encoding))
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
                start_new_session=True, close_fds=True, env=runtime.command_environment(),
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
