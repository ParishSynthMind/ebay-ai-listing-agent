"""Secrets come from the macOS Keychain or the environment, never from a file in the project.

Keychain items (names only, the values are never printed or logged), one set per environment:
  ebay-sandbox-client-id, ebay-sandbox-client-secret, ebay-sandbox-runame,
  ebay-sandbox-refresh-token (written by `ebay-lister exchange-code`),
  and the same four as ebay-production-... for production.
Each can be overridden by an environment variable: EBAY_CLIENT_ID and so on for the sandbox,
EBAY_PROD_CLIENT_ID and so on for production, so a sandbox key can never be used for production by mistake.
"""
from __future__ import annotations

import os
import shutil
import subprocess

ITEMS = {
    "client_id": ("EBAY_CLIENT_ID", "client-id"),
    "client_secret": ("EBAY_CLIENT_SECRET", "client-secret"),
    "runame": ("EBAY_RUNAME", "runame"),
    "refresh_token": ("EBAY_REFRESH_TOKEN", "refresh-token"),
}


def service(name: str, environment: str = "sandbox") -> str:
    return f"ebay-{environment}-{ITEMS[name][1]}"


def env_name(name: str, environment: str = "sandbox") -> str:
    base = ITEMS[name][0]
    return base if environment == "sandbox" else base.replace("EBAY_", "EBAY_PROD_", 1)


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


def secret(name: str, env=os.environ, lookup=keychain_get, environment: str = "sandbox") -> str | None:
    return env.get(env_name(name, environment)) or lookup(service(name, environment))
