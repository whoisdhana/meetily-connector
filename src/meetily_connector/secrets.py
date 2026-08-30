"""Secret resolution: env var, macOS Keychain, or 0600-permission file.

Secrets are never stored in config files — config names only the source.
"""

from __future__ import annotations

import os
import stat
import subprocess


class SecretError(Exception):
    """Secret could not be resolved. Message never contains secret material."""


def resolve_secret(
    source: str,
    *,
    env_var: str = "",
    keychain_service: str = "",
    keychain_account: str = "",
    file_path: str = "",
) -> str:
    if source == "env":
        return _from_env(env_var)
    if source == "keychain":
        return _from_keychain(keychain_service, keychain_account)
    if source == "file":
        return _from_file(file_path)
    raise SecretError(f"unknown secret_source {source!r} (expected env|keychain|file)")


def _from_env(env_var: str) -> str:
    if not env_var:
        raise SecretError("secret_source=env but no env var name configured")
    value = os.environ.get(env_var, "")
    if not value:
        raise SecretError(f"environment variable {env_var} is empty or unset")
    return value


def _from_keychain(service: str, account: str) -> str:
    if not service:
        raise SecretError("secret_source=keychain but no keychain service configured")
    cmd = ["security", "find-generic-password", "-s", service, "-w"]
    if account:
        cmd[2:2] = ["-a", account]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        raise SecretError("`security` CLI not found (keychain source requires macOS)")
    except subprocess.TimeoutExpired:
        raise SecretError("keychain lookup timed out")
    if proc.returncode != 0:
        raise SecretError(f"keychain item not found (service={service!r})")
    value = proc.stdout.rstrip("\n")
    if not value:
        raise SecretError(f"keychain item is empty (service={service!r})")
    return value


def _from_file(path: str) -> str:
    if not path:
        raise SecretError("secret_source=file but no secret_file configured")
    expanded = os.path.expanduser(path)
    try:
        mode = stat.S_IMODE(os.stat(expanded).st_mode)
    except FileNotFoundError:
        raise SecretError(f"secret file {expanded} does not exist")
    if mode & 0o077:
        raise SecretError(
            f"secret file {expanded} has mode {oct(mode)}; refusing anything "
            "readable by group/other — chmod 600 it"
        )
    with open(expanded, encoding="utf-8") as fh:
        value = fh.read().strip()
    if not value:
        raise SecretError(f"secret file {expanded} is empty")
    return value
