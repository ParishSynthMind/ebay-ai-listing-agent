"""Secrets come from the macOS Keychain or the environment, never from a file in the project.

Keychain items (names only, the values are never printed or logged):
  ebay-sandbox-client-id, ebay-sandbox-client-secret, ebay-sandbox-runame,
  ebay-sandbox-refresh-token (written by `ebay-lister exchange-code`).
Each can be overridden by an environment variable (EBAY_CLIENT_ID and so on), which is what CI and Linux use.
"""
from __future__ import annotations

import os
import shutil
import subprocess

ITEMS = {
    "client_id": ("EBAY_CLIENT_ID", "ebay-sandbox-client-id"),
    "client_secret": ("EBAY_CLIENT_SECRET", "ebay-sandbox-client-secret"),
    "runame": ("EBAY_RUNAME", "ebay-sandbox-runame"),
    "refresh_token": ("EBAY_REFRESH_TOKEN", "ebay-sandbox-refresh-token"),
}


def keychain_get(service: str) -> str | None:
    if not shutil.which("security"):
        return None
    r = subprocess.run(["security", "find-generic-password", "-s", service, "-w"], capture_output=True, text=True)
    return r.stdout.strip() or None if r.returncode == 0 else None


def keychain_set(service: str, value: str, account: str = "neo") -> None:
    if not shutil.which("security"):
        raise RuntimeError("no macOS Keychain here; set the environment variable instead")
    subprocess.run(["security", "add-generic-password", "-U", "-a", account, "-s", service, "-w", value],
                   check=True, capture_output=True)


def secret(name: str, env=os.environ, lookup=keychain_get) -> str | None:
    env_name, service = ITEMS[name]
    return env.get(env_name) or lookup(service)
