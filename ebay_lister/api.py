"""Thin clients for the four eBay REST APIs this tool uses. Each method is one documented call.

Taxonomy (and the Metadata API's condition lists) use the application token; Inventory, Account and Fulfillment use the seller's user token.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from .http import EbayApiError, EbayHttp
from .validate import CONDITION_IDS

MARKETPLACE = "EBAY_US"
LANG = {"Content-Language": "en-US", "Content-Type": "application/json"}


def _q(s: str) -> str:
    return quote(str(s), safe="")


class Taxonomy:
    """Category suggestions, the item specifics (aspects) and the conditions each category takes. Cached on disk."""

    def __init__(self, http: EbayHttp, base: str, cache_dir: Optional[Path] = None, cache_days: float = 7.0,
                 marketplace: str = MARKETPLACE):
        self.http = http
        self.base = base + "/commerce/taxonomy/v1"
        self.metadata = base + "/sell/metadata/v1"
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.cache_seconds = cache_days * 86400
        self.marketplace = marketplace
        self._tree: Optional[str] = None

    def tree_id(self) -> str:
        if self._tree is None:
            body = self.http.json("GET", f"{self.base}/get_default_category_tree_id?marketplace_id={self.marketplace}")
            self._tree = body["categoryTreeId"]
        return self._tree

    def suggest(self, query: str, limit: int = 5) -> list[dict]:
        body = self.http.json("GET", f"{self.base}/category_tree/{self.tree_id()}/get_category_suggestions?q={_q(query)}")
        out = []
        for s in body.get("categorySuggestions", [])[:limit]:
            path = [a.get("categoryName", "") for a in reversed(s.get("categoryTreeNodeAncestors", []))]
            out.append({"categoryId": s["category"]["categoryId"], "categoryName": s["category"]["categoryName"],
                        "path": " > ".join([*path, s["category"]["categoryName"]])})
        return out

    def _cached(self, name: str, fetch):
        cache = self.cache_dir / name if self.cache_dir else None
        if cache and cache.exists() and time.time() - cache.stat().st_mtime < self.cache_seconds:
            return json.loads(cache.read_text())
        out = fetch()
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(out))
        return out

    def aspects(self, category_id: str) -> list[dict]:
        return self._cached(f"aspects-{self.tree_id()}-{category_id}.json", lambda: self.http.json(
            "GET", f"{self.base}/category_tree/{self.tree_id()}/get_item_aspects_for_category"
                   f"?category_id={_q(category_id)}").get("aspects", []))

    def conditions(self, category_id: str) -> list[dict]:
        """The conditions a category accepts, as [{"enum": "USED_EXCELLENT", "name": "Used"}] (Metadata API
        getItemConditionPolicies). Found live: Film Cameras takes no USED_GOOD, only plain "Used" (3000)."""
        def fetch():
            body = self.http.json("GET", f"{self.metadata}/marketplace/{self.marketplace}/get_item_condition_policies"
                                         f"?filter={_q('categoryIds:{' + str(category_id) + '}')}")
            out = []
            for pol in body.get("itemConditionPolicies", []):
                for c in pol.get("itemConditions", []):
                    enum = CONDITION_IDS.get(str(c.get("conditionId")))
                    if enum and enum not in [o["enum"] for o in out]:
                        out.append({"enum": enum, "name": c.get("conditionDescription", "")})
            return out
        return self._cached(f"conditions-{self.marketplace}-{category_id}.json", fetch)


class Inventory:
    def __init__(self, http: EbayHttp, base: str):
        self.http = http
        self.base = base + "/sell/inventory/v1"

    # locations
    def get_location(self, key: str) -> Optional[dict]:
        try:
            return self.http.json("GET", f"{self.base}/location/{_q(key)}")
        except EbayApiError as e:
            if e.status == 404:
                return None
            raise

    def create_location(self, key: str, body: dict) -> None:
        self.http.request("POST", f"{self.base}/location/{_q(key)}", headers={"Content-Type": "application/json"},
                          data=json.dumps(body))

    # inventory items (createOrReplace is a PUT, so sending the same item twice is safe)
    def put_item(self, sku: str, item: dict) -> dict:
        return self.http.json("PUT", f"{self.base}/inventory_item/{_q(sku)}", headers=LANG, data=json.dumps(item))

    def get_item(self, sku: str) -> Optional[dict]:
        try:
            return self.http.json("GET", f"{self.base}/inventory_item/{_q(sku)}")
        except EbayApiError as e:
            if e.status == 404:
                return None
            raise

    # offers
    def offers_for(self, sku: str, marketplace: str = MARKETPLACE) -> list[dict]:
        try:
            body = self.http.json("GET", f"{self.base}/offer?sku={_q(sku)}&marketplace_id={marketplace}")
        except EbayApiError as e:
            if e.status == 404:  # eBay answers 404 when the SKU has no offers yet
                return []
            raise
        return body.get("offers", [])

    def get_offer(self, offer_id: str) -> dict:
        return self.http.json("GET", f"{self.base}/offer/{_q(offer_id)}")

    def create_offer(self, offer: dict) -> str:
        return self.http.json("POST", f"{self.base}/offer", headers=LANG, data=json.dumps(offer))["offerId"]

    def update_offer(self, offer_id: str, offer: dict) -> dict:
        return self.http.json("PUT", f"{self.base}/offer/{_q(offer_id)}", headers=LANG, data=json.dumps(offer))

    def publish_offer(self, offer_id: str) -> str:
        return self.http.json("POST", f"{self.base}/offer/{_q(offer_id)}/publish")["listingId"]


class Account:
    """Business policies (shipping, payment, returns) that every offer must point at."""

    def __init__(self, http: EbayHttp, base: str, marketplace: str = MARKETPLACE):
        self.http = http
        self.base = base + "/sell/account/v1"
        self.marketplace = marketplace

    def opted_in(self) -> list[str]:
        return [p["programType"] for p in self.http.json("GET", f"{self.base}/program/get_opted_in_programs").get("programs", [])]

    def opt_in_policies(self) -> None:
        self.http.request("POST", f"{self.base}/program/opt_in", headers={"Content-Type": "application/json"},
                          data=json.dumps({"programType": "SELLING_POLICY_MANAGEMENT"}))

    def policies(self, kind: str) -> list[dict]:
        key = {"fulfillment": "fulfillmentPolicies", "payment": "paymentPolicies", "return": "returnPolicies"}[kind]
        return self.http.json("GET", f"{self.base}/{kind}_policy?marketplace_id={self.marketplace}").get(key, [])

    def create_policy(self, kind: str, body: dict) -> str:
        out = self.http.json("POST", f"{self.base}/{kind}_policy", headers={"Content-Type": "application/json"},
                             data=json.dumps(body))
        return out[f"{kind}PolicyId"]


class Fulfillment:
    def __init__(self, http: EbayHttp, base: str):
        self.http = http
        self.base = base + "/sell/fulfillment/v1"

    def orders(self, status_filter: Optional[str] = None, limit: int = 50, max_pages: int = 20) -> list[dict]:
        """All orders, following eBay's paging. status_filter e.g. "NOT_STARTED|IN_PROGRESS" (orders still to ship)."""
        url = f"{self.base}/order?limit={limit}&offset=0"
        if status_filter:
            url += "&filter=" + _q("orderfulfillmentstatus:{" + status_filter + "}")
        out: list[dict] = []
        for _ in range(max_pages):
            body = self.http.json("GET", url)
            out += body.get("orders", [])
            nxt = body.get("next")
            if not nxt:
                break
            url = nxt if nxt.startswith("http") else self.base.split("/sell/")[0] + nxt
        return out

    def get_order(self, order_id: str) -> dict:
        return self.http.json("GET", f"{self.base}/order/{_q(order_id)}")

    def shipping_fulfillments(self, order_id: str) -> list[dict]:
        return self.http.json("GET", f"{self.base}/order/{_q(order_id)}/shipping_fulfillment").get("fulfillments", [])

    def create_shipping_fulfillment(self, order_id: str, body: dict) -> str:
        """Mark line items shipped. eBay answers 201 with the new fulfillment's URI in the Location header;
        with tracking, the fulfillmentId is the tracking number (eBay's spec)."""
        resp = self.http.request("POST", f"{self.base}/order/{_q(order_id)}/shipping_fulfillment",
                                 headers={"Content-Type": "application/json"}, data=json.dumps(body))
        loc = (getattr(resp, "headers", None) or {}).get("Location", "")
        return loc.rstrip("/").rsplit("/", 1)[-1] if loc else ""
