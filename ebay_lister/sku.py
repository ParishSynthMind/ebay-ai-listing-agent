"""SKUs that are the same every run, so re-running a CSV updates listings instead of making duplicates.

A SKU given in the CSV is used as it is (after a check). Otherwise one is made from what identifies
the product (brand, model/MPN, the seller's own reference or the note), never from the AI's wording,
which can change between runs.
"""
from __future__ import annotations

import hashlib
import re

MAX_LEN = 50  # eBay's limit for an inventory item SKU
VALID = re.compile(r"^[A-Za-z0-9._-]{1,50}$")


def check_sku(sku: str) -> str:
    sku = sku.strip()
    if not VALID.match(sku):
        raise ValueError(f"SKU {sku!r} must be 1 to {MAX_LEN} letters, digits, dot, dash or underscore")
    return sku


def make_sku(row: dict, prefix: str = "AI") -> str:
    if (row.get("sku") or "").strip():
        return check_sku(row["sku"])
    parts = [row.get(k, "") for k in ("brand", "mpn", "model", "ref", "note", "photos")]
    key = "|".join(" ".join(str(p).lower().split()) for p in parts)
    if not key.strip("|"):
        raise ValueError("cannot make a SKU from an empty row")
    return f"{prefix}-{hashlib.sha256(key.encode()).hexdigest()[:12].upper()}"
