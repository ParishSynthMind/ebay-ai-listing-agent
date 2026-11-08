"""Local record of what was listed, so re-runs update instead of duplicating and repricing knows when an item went live."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
  sku TEXT PRIMARY KEY, status TEXT, title TEXT, price REAL, currency TEXT, category_id TEXT,
  offer_id TEXT, listing_id TEXT, first_listed TEXT, updated TEXT, draft TEXT, problems TEXT, input_hash TEXT
);
CREATE TABLE IF NOT EXISTS events (at TEXT, sku TEXT, what TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS approvals (sku TEXT PRIMARY KEY, input_hash TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS shipments (order_id TEXT, tracking TEXT, carrier TEXT, fulfillment_id TEXT, at TEXT,
  PRIMARY KEY (order_id, tracking));
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, path: Path | str = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def get(self, sku: str) -> dict | None:
        r = self.db.execute("SELECT * FROM listings WHERE sku=?", (sku,)).fetchone()
        return dict(r) if r else None

    def save(self, sku: str, status: str, listing: dict | None = None, problems: list | None = None,
             offer_id: str | None = None, listing_id: str | None = None, input_hash: str | None = None) -> None:
        old = self.get(sku) or {}
        listing = listing or (json.loads(old["draft"]) if old.get("draft") else {})
        first = old.get("first_listed") or (now_iso() if listing_id else None)
        self.db.execute(
            "INSERT OR REPLACE INTO listings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sku, status, listing.get("title"), listing.get("price"), listing.get("currency"), listing.get("category_id"),
             offer_id or old.get("offer_id"), listing_id or old.get("listing_id"), first, now_iso(),
             json.dumps(listing), json.dumps([str(p) for p in (problems or [])]), input_hash or old.get("input_hash")))
        self.db.commit()

    def set_price(self, sku: str, price: float) -> None:
        self.db.execute("UPDATE listings SET price=?, updated=? WHERE sku=?", (price, now_iso(), sku))
        self.db.commit()

    def event(self, sku: str, what: str, detail: str = "") -> None:
        self.db.execute("INSERT INTO events VALUES (?,?,?,?)", (now_iso(), sku, what, detail))
        self.db.commit()

    def all(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM listings ORDER BY sku")]

    def events(self, sku: str | None = None) -> list[dict]:
        q = "SELECT * FROM events" + (" WHERE sku=?" if sku else "") + " ORDER BY rowid"
        return [dict(r) for r in self.db.execute(q, (sku,) if sku else ())]

    def approve(self, sku: str) -> bool:
        """A person has checked this draft. The approval is tied to the input it was drafted from, so a changed
        row needs a fresh look."""
        row = self.get(sku)
        if not row or not row.get("input_hash"):
            return False
        self.db.execute("INSERT OR REPLACE INTO approvals VALUES (?,?,?)", (sku, row["input_hash"], now_iso()))
        self.db.commit()
        return True

    def approved(self, sku: str, input_hash: str) -> bool:
        r = self.db.execute("SELECT input_hash FROM approvals WHERE sku=?", (sku,)).fetchone()
        return bool(r and r[0] == input_hash)

    def shipped(self, order_id: str, tracking: str, carrier: str, fulfillment_id: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO shipments VALUES (?,?,?,?,?)",
                        (order_id, tracking, carrier, fulfillment_id, now_iso()))
        self.db.commit()
