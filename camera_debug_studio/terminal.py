"""Persistent local/SSH terminal session lifecycle."""

from __future__ import annotations

import codecs
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
        self.winpty_process: Any = None
        self.process: Any = None
        self.backend = "pipe"
        target = runtime.config.get("target", {})
        default_encoding = "utf-8" if target.get("transport", "ssh") == "ssh" else locale.getpreferredencoding(False)
        self.encoding = str(target.get("terminalEncoding", default_encoding))
        command = runtime.terminal_command()
        if os.name == "nt":
            try:
                from winpty import PtyProcess

                self.winpty_process = PtyProcess.spawn(
                    command, env=runtime.command_environment(), dimensions=(30, 100)
                )
                self.backend = "conpty"
                threading.Thread(target=self._pump_winpty, daemon=True).start()
                return
            except ImportError:
                self.send({
                    "type": "output",
                    "data": "\r\n\x1b[33m[提示] 未安装 pywinpty，Windows 终端正在使用兼容模式。\x1b[0m\r\n",
                })
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
            self.backend = "pty"

    def _pump_winpty(self) -> None:
        try:
            while not self.closed.is_set():
                try:
                    data = self.winpty_process.read(4096)
                except EOFError:
                    break
                if data:
                    self.send({"type": "output", "data": data})
        finally:
            try:
                code = self.winpty_process.wait()
            except Exception:
                code = self.winpty_process.exitstatus
            if not self.closed.is_set():
                try:
                    self.send({"type": "exit", "code": code})
                except OSError:
                    pass
            try:
                self.winpty_process.close()
            except Exception:
                pass
            self.closed.set()

    def _emit_decoded(self, decoder: Any, chunk: bytes, final: bool = False) -> None:
        data = decoder.decode(chunk, final=final)
        if data:
            self.send({"type": "output", "data": data})

    def _pump_pty(self) -> None:
        decoder = codecs.getincrementaldecoder(self.encoding)(errors="replace")
        try:
            while not self.closed.is_set():
                chunk = os.read(self.master_fd, 4096)  # type: ignore[arg-type]
                if not chunk:
                    break
                self._emit_decoded(decoder, chunk)
        except OSError:
            pass
        finally:
            self._emit_decoded(decoder, b"", final=True)
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
        decoder = codecs.getincrementaldecoder(self.encoding)(errors="replace")
        try:
            while not self.closed.is_set():
                chunk = stream.read1(4096) if hasattr(stream, "read1") else stream.read(4096)
                if not chunk:
                    break
                self._emit_decoded(decoder, chunk)
        except OSError:
            pass
        finally:
            self._emit_decoded(decoder, b"", final=True)

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
        if self.winpty_process is not None:
            if data == "\x03":
                self.winpty_process.sendintr()
            else:
                self.winpty_process.write(data)
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
        columns = max(20, min(400, columns))
        rows = max(5, min(200, rows))
        if self.winpty_process is not None:
            self.winpty_process.setwinsize(rows, columns)
            return
        if os.name == "nt" or self.master_fd is None:
            return
        import fcntl
        import termios
        fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))

    def close(self) -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        if self.winpty_process is not None:
            try:
                self.winpty_process.terminate(force=True)
            except Exception:
                pass
            return
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
