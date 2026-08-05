"""Local and SSH process command construction."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List


def command_environment(config: Dict[str, Any], root: Path) -> Dict[str, str]:
    environment = dict(os.environ)
    password = str(config.get("target", {}).get("password", ""))
    if config.get("target", {}).get("transport") == "ssh" and password:
        askpass_script = str(root / "ssh_askpass.py")
        askpass = (subprocess.list2cmdline([sys.executable, askpass_script])
                   if os.name == "nt" else askpass_script)
        environment.update({
            "SSH_ASKPASS": askpass,
            "SSH_ASKPASS_REQUIRE": "force",
            "CAMERA_DEBUG_SSH_PASSWORD": password,
            "DISPLAY": environment.get("DISPLAY", "camera-debug:0"),
        })
    return environment


def _ssh_base(target: Dict[str, Any], interactive: bool) -> List[str]:
    host = target.get("host", "")
    if not host:
        raise ValueError("SSH host 未配置")
    user = target.get("user", "")
    destination = f"{user}@{host}" if user else host
    argv = [target.get("sshBinary", "ssh")]
    if interactive:
        argv.append("-tt")
    argv += ["-p", str(target.get("port", 22)), "-o", f"ConnectTimeout={int(target.get('connectTimeout', 8))}",
             "-o", "ServerAliveInterval=15"]
    if target.get("identityFile"):
        argv += ["-i", os.path.expanduser(target["identityFile"])]
    for option in target.get("sshOptions", []):
        argv += ["-o", str(option)]
    if target.get("password"):
        argv += ["-o", "BatchMode=no", "-o", "NumberOfPasswordPrompts=1"]
    else:
        argv += ["-o", "BatchMode=yes"]
    return argv + [destination]


def transport_command(config: Dict[str, Any], remote_command: str) -> List[str]:
    target = config.get("target", {})
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
        return [str(shell), "-lc", remote_command] if shell else ["sh", "-lc", remote_command]
    if mode != "ssh":
        raise ValueError(f"不支持的 transport: {mode}")
    return _ssh_base(target, False) + [remote_command]


def scp_upload_command(config: Dict[str, Any], local_path: str, remote_path: str) -> List[str]:
    """Build an SCP upload command using the active SSH target credentials."""
    target = config.get("target", {})
    if target.get("transport", "ssh") != "ssh":
        raise ValueError("Shell 脚本只能上传到 SSH 目标")
    host = target.get("host", "")
    if not host:
        raise ValueError("SSH host 未配置")
    user = target.get("user", "")
    destination = f"{user}@{host}" if user else host
    argv = [target.get("scpBinary", "scp"), "-P", str(target.get("port", 22)),
            "-o", f"ConnectTimeout={int(target.get('connectTimeout', 8))}"]
    if target.get("identityFile"):
        argv += ["-i", os.path.expanduser(target["identityFile"])]
    for option in target.get("sshOptions", []):
        argv += ["-o", str(option)]
    if target.get("password"):
        argv += ["-o", "BatchMode=no", "-o", "NumberOfPasswordPrompts=1"]
    else:
        argv += ["-o", "BatchMode=yes"]
    return argv + [local_path, f"{destination}:{remote_path}"]


def terminal_command(config: Dict[str, Any]) -> List[str]:
    target = config.get("target", {})
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
    return _ssh_base(target, True)
