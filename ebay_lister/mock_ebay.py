"""An in-memory eBay sandbox: the same URLs, JSON and error bodies as the real APIs, for the calls this tool makes.

It is a session object with a requests-style .request(method, url, headers=, data=, timeout=), so the real
client code runs against it unchanged. Shapes and error ids come from eBay's published OpenAPI specs
(inventory_api.json v1.18, taxonomy_api.json v1.1, account_api_v1.json v1.9, fulfillment_api.json v1.20).
See docs/EBAY-FACTS.md for which details are confirmed from the specs and which are the mock's best guess.

Rules it enforces, because they are what trips people up on the real thing:
- OAuth: Basic client auth on the token endpoint; one-time authorization codes; redirect_uri must be the RuName;
  access tokens expire (7200 s); an application token cannot call the Sell APIs (403, errorId 1100).
- Inventory: Content-Language header required on item and offer writes; SKU max 50 characters, letters and digits;
  title max 80; only one offer per SKU + marketplace + format (25002 "Offer entity already exists.").
- publishOffer checks what eBay checks: business policies exist (25007 to 25009, or 25017 if missing), the
  location exists (25012), a leaf category (25005), a price (25003), a quantity (25004), at least one picture
  (25014), a condition the category takes (25021) and every required item specific of the category (25002, "The item specific
  Brand is missing...").
- Metadata getItemConditionPolicies lists each category's conditions by id.
- Business policies need the seller opted in to SELLING_POLICY_MANAGEMENT first.
- getOrders pages with limit (max 200) and offset and supports filter=orderfulfillmentstatus:{A|B}.
- createShippingFulfillment answers 201 with a Location header, refuses a bad order id (32100), line item (32200),
  duplicate line item (32210), tracking without carrier or with spaces or hyphens (32300) and a bad date (32500),
  then marks the line items FULFILLED and the order FULFILLED or IN_PROGRESS.
- 429 (errorId 2002) and 500 (25001) can be injected to test backoff.
"""
from __future__ import annotations

import base64
import copy
import itertools
import json
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

from .auth import APP_SCOPE, USER_SCOPES
from .validate import CONDITION_IDS

SKU_RX = re.compile(r"^[A-Za-z0-9._-]{1,50}$")


class FakeResponse:
    def __init__(self, status: int, body=None, headers: Optional[dict] = None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = "" if body is None else json.dumps(body)

    def json(self):
        if self._body is None:
            raise ValueError("no JSON body")
        return self._body


def err(status: int, error_id: int, message: str, domain: str = "API_INVENTORY", category: str = "REQUEST",
        long: str = "", params: Optional[list] = None) -> FakeResponse:
    e = {"errorId": error_id, "domain": domain, "category": category, "message": message}
    if long:
        e["longMessage"] = long
    if params:
        e["parameters"] = params
    return FakeResponse(status, {"errors": [e]})


def user_error(info: str, params: Optional[list] = None) -> FakeResponse:
    return err(400, 25002, f"A user error has occurred. {info}", params=params)


def _aspect(name, required=False, mode="FREE_TEXT", single=True, values=(), usage=None, max_len=None):
    c = {"aspectDataType": "STRING", "aspectMode": mode, "aspectRequired": required,
         "aspectUsage": usage or ("RECOMMENDED" if required else "OPTIONAL"),
         "itemToAspectCardinality": "SINGLE" if single else "MULTI", "aspectEnabledForVariations": False}
    if max_len:
        c["aspectMaxLength"] = max_len
    return {"localizedAspectName": name, "aspectConstraint": c,
            "aspectValues": [{"localizedValue": v} for v in values]}


SHOE_SIZES = [str(x / 2).rstrip("0").rstrip(".") for x in range(4, 41)]
# A small slice of the EBAY_US tree, copied from the live sandbox on 9 November 2026 (tree version 134):
# real category ids and ancestors, the real required item specifics with their modes and cardinality, a trimmed
# set of optional ones, and each category's conditions from getItemConditionPolicies.
CATEGORIES = {
    "11450": {"name": "Clothing, Shoes & Accessories", "leaf": False, "ancestors": [], "keywords": []},
    "260012": {"name": "Men", "leaf": False, "ancestors": ["11450"], "keywords": []},
    "93427": {"name": "Men's Shoes", "leaf": False, "ancestors": ["11450", "260012"], "keywords": []},
    "15709": {
        "name": "Athletic Shoes", "leaf": True, "ancestors": ["11450", "260012", "93427"],
        "keywords": ["running", "trainers", "sneakers", "shoes", "athletic", "shoe", "trainer", "sneaker"],
        "conditions": [("1000", "New with box"), ("1500", "New without box"), ("1750", "New with defects"),
                       ("2990", "Pre-owned - Excellent"), ("3000", "Pre-owned - Good"), ("3010", "Pre-owned - Fair")],
        "aspects": [
            _aspect("Brand", True, values=["Unbranded", "adidas", "ASICS", "Brooks", "New Balance", "Nike"], usage="RECOMMENDED"),
            _aspect("Color", True, values=["Beige", "Black", "Blue", "Gray", "Multicolor", "Red", "White"], usage="RECOMMENDED"),
            _aspect("Department", True, "SELECTION_ONLY", values=["Men", "Teens", "Unisex Adults"], usage="RECOMMENDED"),
            _aspect("US Shoe Size", True, values=SHOE_SIZES, usage="RECOMMENDED"),
            _aspect("Type", mode="SELECTION_ONLY", values=["Athletic", "Cleats", "Sneakers"], usage="RECOMMENDED"),
            _aspect("Style", mode="SELECTION_ONLY", values=["Casual", "Classic", "Modern", "Retro"], usage="RECOMMENDED"),
            _aspect("Model", usage="RECOMMENDED"),
            _aspect("Upper Material", values=["Canvas", "Leather", "Mesh", "Synthetic"], usage="RECOMMENDED"),
            _aspect("Shoe Width", values=["Standard", "Wide", "Narrow"]),
        ],
    },
    "625": {"name": "Cameras & Photo", "leaf": False, "ancestors": [], "keywords": []},
    "69323": {"name": "Film Photography", "leaf": False, "ancestors": ["625"], "keywords": []},
    "15230": {
        "name": "Film Cameras", "leaf": True, "ancestors": ["625", "69323"],
        "keywords": ["film", "camera", "35mm", "slr", "rangefinder", "cameras"],
        "conditions": [("1000", "New"), ("1500", "Open box"), ("2500", "Seller refurbished"), ("3000", "Used"),
                       ("7000", "For parts or not working")],
        "aspects": [
            _aspect("Brand", True, values=["Unbranded", "Canon", "Leica", "Minolta", "Nikon", "Olympus", "Pentax"], usage="RECOMMENDED"),
            _aspect("Type", True, values=["35mm", "Compact", "Instant", "Medium Format", "Rangefinder", "SLR", "TLR"], usage="RECOMMENDED"),
            _aspect("Model", True, usage="RECOMMENDED"),
            _aspect("MPN", single=False, usage="RECOMMENDED"),
            _aspect("Format", values=["35mm", "120 mm", "APS", "Instax Mini"], usage="RECOMMENDED"),
            _aspect("Focus Type", values=["Auto", "Auto & Manual", "Eye-Controlled", "Fixed", "Manual"], usage="RECOMMENDED"),
            _aspect("Color", values=["Black", "Gray", "Silver"], usage="RECOMMENDED"),
        ],
    },
    "11700": {"name": "Home & Garden", "leaf": False, "ancestors": [], "keywords": []},
    "20625": {"name": "Kitchen, Dining & Bar", "leaf": False, "ancestors": ["11700"], "keywords": []},
    "36027": {"name": "Dinnerware & Serveware", "leaf": False, "ancestors": ["11700", "20625"], "keywords": []},
    "20695": {
        "name": "Mugs", "leaf": True, "ancestors": ["11700", "20625", "36027"],
        "keywords": ["mug", "mugs", "cup", "coffee", "tea", "ceramic"],
        "conditions": [("1000", "New"), ("1500", "New other (see details)"), ("3000", "Used")],
        "aspects": [
            _aspect("Type", True, values=["Coffee Mug", "Beer Mug", "Latte Mug", "Novelty Mug", "Travel Mug"], usage="RECOMMENDED"),
            _aspect("Brand", True, values=["Unbranded", "Handmade"], usage="RECOMMENDED"),
            _aspect("Color", True, values=["Black", "Blue", "Green", "Multicolor", "White"], usage="RECOMMENDED"),
            _aspect("Material", single=False, values=["Ceramic", "Glass", "Porcelain", "Stoneware"], usage="RECOMMENDED"),
            _aspect("Capacity", values=["8 fl oz", "10 fl oz", "12 fl oz", "16 fl oz"], usage="RECOMMENDED"),
            _aspect("Personalize", mode="SELECTION_ONLY", values=["Yes", "No"], usage="RECOMMENDED"),
        ],
    },
    "11116": {"name": "Coins & Paper Money", "leaf": False, "ancestors": [], "keywords": []},
}
CONDITION_ENUM_IDS = {v: k for k, v in CONDITION_IDS.items()}  # Inventory API name -> condition id


@dataclass
class Offer:
    offerId: str
    body: dict
    status: str = "UNPUBLISHED"
    listingId: Optional[str] = None


@dataclass
class FakeEbay:
    client_id: str = "TestApp-SBX-0000-sandbox"
    client_secret: str = "SBX-test-secret"
    runame: str = "Test_Seller-TestApp-SBX-abcdef"
    opted_in: bool = False
    now: float = 1_000_000.0
    tokens: dict = field(default_factory=dict)          # token -> {"kind", "expires", "scopes"}
    codes: set = field(default_factory=set)
    refresh_tokens: set = field(default_factory=set)
    items: dict = field(default_factory=dict)
    offers: dict = field(default_factory=dict)
    locations: dict = field(default_factory=dict)
    policies: dict = field(default_factory=lambda: {"fulfillment": {}, "payment": {}, "return": {}})
    orders: list = field(default_factory=list)
    fulfillments: dict = field(default_factory=dict)     # orderId -> shipping fulfillments
    log: list = field(default_factory=list)              # (method, path) of every call
    inject: list = field(default_factory=list)           # FakeResponses returned before normal handling, in order
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))

    # --- helpers for tests and demos ------------------------------------------
    def issue_code(self) -> str:
        code = f"v^1.1#i^1#code{next(self._ids)}"
        self.codes.add(code)
        return code

    def expire_tokens(self) -> None:
        for t in self.tokens.values():
            t["expires"] = self.now - 1

    def add_order(self, sku: str, title: str, price: float, status: str = "NOT_STARTED", created: str = "2026-11-01T10:00:00.000Z",
                  buyer: str = "testuser_buyer1", quantity: int = 1) -> dict:
        n = next(self._ids)
        o = {"orderId": f"12-{n:05d}-{n * 7:05d}", "creationDate": created, "lastModifiedDate": created,
             "orderFulfillmentStatus": status, "orderPaymentStatus": "PAID", "sellerId": "testuser_seller1",
             "buyer": {"username": buyer},
             "pricingSummary": {"total": {"value": f"{price * quantity:.2f}", "currency": "USD"}},
             "lineItems": [{"lineItemId": f"{n}0001", "sku": sku, "title": title, "quantity": quantity,
                            "lineItemCost": {"value": f"{price * quantity:.2f}", "currency": "USD"},
                            "lineItemFulfillmentStatus": status, "legacyItemId": f"1100{n:08d}"}]}
        self.orders.append(o)
        return o

    def calls(self, method: str, prefix: str) -> int:
        return sum(1 for m, p in self.log if m == method and p.startswith(prefix))

    # --- dispatch --------------------------------------------------------------
    def request(self, method: str, url: str, headers: Optional[dict] = None, data=None, timeout=None, **_):
        u = urlparse(url)
        path = unquote(u.path)
        self.log.append((method, path))
        if self.inject:
            return self.inject.pop(0)
        query = {k: v[0] for k, v in parse_qs(u.query).items()}
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        if u.netloc not in ("api.sandbox.ebay.com", "apiz.sandbox.ebay.com"):
            return err(404, 0, f"mock only serves the sandbox hosts, not {u.netloc}")
        if path == "/identity/v1/oauth2/token":
            return self._token(headers, data)
        if u.netloc == "apiz.sandbox.ebay.com" and path.startswith("/sell/fulfillment/"):
            # fulfillment_api.json names apiz, but the live sandbox answers this 404 there (9 Nov 2026)
            return FakeResponse(404, {"errors": [{"errorId": 2002, "domain": "ACCESS", "category": "REQUEST",
                                                  "message": "Resource not found",
                                                  "longMessage": "A resource (URI) associated with the request could not be resolved.",
                                                  "parameters": [{"name": "reason", "value": "HTTP 404 Not Found"}]}]})
        body = json.loads(data) if isinstance(data, (str, bytes)) and data else None
        if path.startswith("/commerce/taxonomy/v1"):
            denied = self._auth(headers, needs_user=False)
            return denied or self._taxonomy(path[len("/commerce/taxonomy/v1"):], query)
        if path.startswith("/sell/metadata/v1"):
            denied = self._auth(headers, needs_user=False)
            return denied or self._metadata(path[len("/sell/metadata/v1"):], query)
        denied = self._auth(headers, needs_user=True)
        if denied:
            return denied
        if path.startswith("/sell/inventory/v1"):
            return self._inventory(method, path[len("/sell/inventory/v1"):], query, headers, body)
        if path.startswith("/sell/account/v1"):
            return self._account(method, path[len("/sell/account/v1"):], query, body)
        if path.startswith("/sell/fulfillment/v1"):
            return self._fulfillment(method, path[len("/sell/fulfillment/v1"):], query, url, body)
        return err(404, 0, f"mock has no route for {method} {path}")

    # --- OAuth -------------------------------------------------------------------
    def _token(self, headers, data):
        auth = headers.get("authorization", "")
        good = "Basic " + base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        if auth != good:
            return FakeResponse(401, {"error": "invalid_client", "error_description": "client authentication failed"})
        form = data if isinstance(data, dict) else {k: v[0] for k, v in parse_qs(data or "").items()}
        grant = form.get("grant_type")
        n = next(self._ids)
        if grant == "client_credentials":
            scopes = (form.get("scope") or "").split()
            if any(s != APP_SCOPE for s in scopes):
                return FakeResponse(400, {"error": "invalid_scope",
                                          "error_description": "The requested scope is invalid, unknown, malformed, or exceeds the scope granted to the client"})
            tok = f"v^1.1#i^1#app{n}"
            self.tokens[tok] = {"kind": "app", "expires": self.now + 7200, "scopes": [APP_SCOPE]}
            return FakeResponse(200, {"access_token": tok, "expires_in": 7200, "token_type": "Application Access Token"})
        if grant == "authorization_code":
            if form.get("code") not in self.codes or form.get("redirect_uri") != self.runame:
                return FakeResponse(400, {"error": "invalid_grant",
                                          "error_description": "the provided authorization grant code is invalid or was issued to another client"})
            self.codes.discard(form["code"])
            rt = f"v^1.1#i^1#refresh{n}"
            self.refresh_tokens.add(rt)
            tok = f"v^1.1#i^1#user{n}"
            self.tokens[tok] = {"kind": "user", "expires": self.now + 7200, "scopes": USER_SCOPES}
            return FakeResponse(200, {"access_token": tok, "expires_in": 7200, "refresh_token": rt,
                                      "refresh_token_expires_in": 47304000, "token_type": "User Access Token"})
        if grant == "refresh_token":
            if form.get("refresh_token") not in self.refresh_tokens:
                return FakeResponse(400, {"error": "invalid_grant",
                                          "error_description": "the provided authorization refresh token is invalid or was issued to another client"})
            scopes = (form.get("scope") or "").split()
            if any(s not in USER_SCOPES for s in scopes):
                return FakeResponse(400, {"error": "invalid_scope",
                                          "error_description": "The requested scope is invalid, unknown, malformed, or exceeds the scope granted to the client"})
            tok = f"v^1.1#i^1#user{n}"
            self.tokens[tok] = {"kind": "user", "expires": self.now + 7200, "scopes": scopes or USER_SCOPES}
            return FakeResponse(200, {"access_token": tok, "expires_in": 7200, "token_type": "User Access Token"})
        return FakeResponse(400, {"error": "unsupported_grant_type", "error_description": "grant type in request is not supported by the authorization server"})

    def _auth(self, headers, needs_user: bool):
        h = headers.get("authorization", "")
        tok = self.tokens.get(h[7:] if h.startswith("Bearer ") else "")
        if tok is None or tok["expires"] <= self.now:
            return err(401, 1001, "Invalid access token", domain="OAuth",
                       long="Invalid access token. Check the value of the Authorization HTTP request header.")
        if needs_user and tok["kind"] != "user":
            return err(403, 1100, "Access denied", domain="ACCESS", long="Insufficient permissions to fulfill the request.")
        return None

    # --- Metadata (condition policies) ---------------------------------------------------
    def _metadata(self, path, q):
        m = re.fullmatch(r"/marketplace/([A-Z_]+)/get_item_condition_policies", path)
        if not m:
            return err(404, 0, f"mock has no metadata route {path}")
        ids = re.fullmatch(r"categoryIds:\{([0-9|]+)\}", q.get("filter", ""))
        wanted = ids.group(1).split("|") if ids else [c for c, v in CATEGORIES.items() if v["leaf"]]
        pols = [{"categoryId": cid, "categoryTreeId": "0", "itemConditionRequired": True,
                 "itemConditions": [{"conditionId": i, "conditionDescription": n, "usage": "REQUIRED"}
                                    for i, n in CATEGORIES[cid]["conditions"]]}
                for cid in wanted if cid in CATEGORIES and CATEGORIES[cid]["leaf"]]
        return FakeResponse(200, {"itemConditionPolicies": pols})

    # --- Taxonomy -------------------------------------------------------------------
    def _taxonomy(self, path, q):
        if path == "/get_default_category_tree_id":
            if not q.get("marketplace_id"):
                return err(400, 62002, "Missing marketplace ID.", domain="API_TAXONOMY")
            return FakeResponse(200, {"categoryTreeId": "0", "categoryTreeVersion": "134"})
        m = re.fullmatch(r"/category_tree/([^/]+)/(get_category_suggestions|get_item_aspects_for_category)", path)
        if not m or m.group(1) != "0":
            return err(404, 62000, "The specified category tree ID was not found.", domain="API_TAXONOMY")
        if m.group(2) == "get_category_suggestions":
            if not q.get("q"):
                return err(400, 62007, "Missing keyword 'q'. Please specify a valid keyword.", domain="API_TAXONOMY")
            words = set(re.findall(r"[a-z0-9]+", q["q"].lower()))
            scored = sorted(((len(words & set(c["keywords"])), cid) for cid, c in CATEGORIES.items() if c["leaf"]), reverse=True)
            sugg = []
            for score, cid in scored:
                if score == 0:
                    continue
                c = CATEGORIES[cid]
                anc = [{"categoryId": a, "categoryName": CATEGORIES[a]["name"], "categoryTreeNodeLevel": i + 1}
                       for i, a in enumerate(c["ancestors"])]
                sugg.append({"category": {"categoryId": cid, "categoryName": c["name"]},
                             "categoryTreeNodeAncestors": list(reversed(anc)), "categoryTreeNodeLevel": len(anc) + 1})
            if not sugg:
                return FakeResponse(204)
            return FakeResponse(200, {"categorySuggestions": sugg, "categoryTreeId": "0", "categoryTreeVersion": "134"})
        cid = q.get("category_id", "")
        c = CATEGORIES.get(cid)
        if c is None:
            return err(400, 62005, "The specified category ID does not belong to specified category tree.", domain="API_TAXONOMY")
        if not c["leaf"]:
            return err(400, 62009, "The specified category ID must be a leaf category.", domain="API_TAXONOMY")
        return FakeResponse(200, {"aspects": copy.deepcopy(c["aspects"])})

    # --- Inventory ----------------------------------------------------------------------
    def _inventory(self, method, path, q, headers, body):
        m = re.fullmatch(r"/location/([^/]+)", path)
        if m:
            key = m.group(1)
            if method == "GET":
                if key not in self.locations:
                    return err(404, 25804, "Input error. Location not found.")
                return FakeResponse(200, {"merchantLocationKey": key, **self.locations[key]})
            if method == "POST":
                if key in self.locations:
                    return err(409, 25803, "merchantLocationKey already exists.")
                if not (body or {}).get("location", {}).get("address"):
                    return err(400, 25801, "Missing field location.address.")
                self.locations[key] = body
                return FakeResponse(204)
        m = re.fullmatch(r"/inventory_item/([^/]+)", path)
        if m:
            sku = m.group(1)
            if not SKU_RX.match(sku):
                return err(400, 25707, "Invalid sku. sku has to be alphanumeric with upto 50 characters in length")
            if method == "GET":
                if sku not in self.items:
                    return err(404, 25710, "We didn't find the resource/entity you are requesting. Please verify the request")
                return FakeResponse(200, {"sku": sku, **copy.deepcopy(self.items[sku])})
            if method == "PUT":
                if "content-language" not in headers:
                    return err(400, 25709, "Invalid value for header Content-Language. ")
                title = (body or {}).get("product", {}).get("title", "")
                if len(title) > 80:
                    return err(400, 25709, "Invalid value for product.title. Maximum length is 80.")
                for name, vals in (body or {}).get("product", {}).get("aspects", {}).items():
                    if len(name) > 40 or any(len(v) > 50 for v in vals):
                        return err(400, 25022, f"Invalid attribute. product.aspects.{name}")
                existed = sku in self.items
                self.items[sku] = copy.deepcopy(body)
                for o in self.offers.values():  # a published listing picks up item changes, as on eBay
                    if o.body["sku"] == sku and o.status == "PUBLISHED":
                        o.body["_item_revised"] = True
                return FakeResponse(204 if existed else 201, None if existed else {"warnings": []})
        if path == "/offer" and method == "GET":
            sku = q.get("sku")
            if not sku:
                return err(400, 25709, "Invalid value for sku. ")
            found = [self._offer_json(o) for o in self.offers.values() if o.body["sku"] == sku
                     and o.body.get("marketplaceId") == q.get("marketplace_id", o.body.get("marketplaceId"))]
            if not found:
                return err(404, 25713, "This Offer is not available : No offer found.")
            return FakeResponse(200, {"href": "", "limit": 25, "offers": found, "size": len(found), "total": len(found)})
        if path == "/offer" and method == "POST":
            if "content-language" not in headers:
                return err(400, 25709, "Invalid value for header Content-Language. ")
            b = body or {}
            for k in ("sku", "marketplaceId", "format"):
                if not b.get(k):
                    return err(400, 25709, f"Invalid value for {k}. ")
            if b["sku"] not in self.items:
                return err(400, 25702, f"{b['sku']} could not be found or is not available in the system.")
            for o in self.offers.values():
                if (o.body["sku"], o.body["marketplaceId"], o.body["format"]) == (b["sku"], b["marketplaceId"], b["format"]):
                    return user_error("Offer entity already exists.", [{"name": "offerId", "value": o.offerId}])
            oid = str(5_000_000_000 + next(self._ids))
            self.offers[oid] = Offer(oid, copy.deepcopy(b))
            return FakeResponse(201, {"offerId": oid, "warnings": []})
        m = re.fullmatch(r"/offer/([^/]+)(/publish)?", path)
        if m:
            o = self.offers.get(m.group(1))
            if o is None:
                return err(404, 25713, "This Offer is not available : Invalid offerId.")
            if m.group(2):
                return self._publish(o) if method == "POST" else err(405, 0, "method not allowed")
            if method == "GET":
                return FakeResponse(200, self._offer_json(o))
            if method == "PUT":
                if "content-language" not in headers:
                    return err(400, 25709, "Invalid value for header Content-Language. ")
                b = copy.deepcopy(body or {})
                b.setdefault("sku", o.body["sku"])
                b.setdefault("marketplaceId", o.body["marketplaceId"])
                b.setdefault("format", o.body["format"])
                if o.status == "PUBLISHED":  # a published offer is revised live; eBay re-checks it
                    bad = self._publish_problems(Offer(o.offerId, b, o.status, o.listingId))
                    if bad:
                        return bad
                o.body = b
                return FakeResponse(200, {"offerId": o.offerId, "warnings": []})
        return err(404, 0, f"mock has no route for {method} /sell/inventory/v1{path}")

    def _offer_json(self, o: Offer) -> dict:
        j = {k: v for k, v in copy.deepcopy(o.body).items() if not k.startswith("_")}
        j.update({"offerId": o.offerId, "status": o.status})
        if o.listingId:
            j["listing"] = {"listingId": o.listingId, "listingStatus": "ACTIVE"}
        return j

    def _publish_problems(self, o: Offer) -> Optional[FakeResponse]:
        b = o.body
        item = self.items.get(b["sku"])
        if item is None:
            return err(400, 25702, f"{b['sku']} could not be found or is not available in the system.")
        pol = b.get("listingPolicies") or {}
        for kind, eid, label in (("fulfillment", 25007, "Shipping"), ("payment", 25008, "Payment"), ("return", 25009, "Return")):
            pid = pol.get(f"{kind}PolicyId")
            if not pid:
                return err(400, 25017, f"{label} policy is required.")
            if pid not in self.policies[kind]:
                what = {"fulfillment": "Fulfillment", "payment": "Payment", "return": "Return"}[kind]
                return err(400, eid, f"The eBay listing associated with the inventory item, or the unpublished offer has invalid data in the associated {what} policy. ")
        if b.get("merchantLocationKey") not in self.locations:
            return err(400, 25012, "Invalid inventory location. No Item.Country exists.")
        cat = CATEGORIES.get(str(b.get("categoryId", "")))
        if cat is None or not cat["leaf"]:
            return err(400, 25005, "The eBay listing associated with the inventory item, or the unpublished offer has an invalid category ID. ")
        price = (b.get("pricingSummary") or {}).get("price") or {}
        try:
            ok_price = float(price.get("value", 0)) >= 0.99
        except (TypeError, ValueError):
            ok_price = False
        if not ok_price:
            return err(400, 25003, "The eBay listing associated with the inventory item, or the unpublished offer has an invalid price. ")
        qty = (item.get("availability") or {}).get("shipToLocationAvailability", {}).get("quantity", 0)
        if not qty or qty < 1:
            return err(400, 25004, "The eBay listing associated with the inventory item, or the unpublished offer has an invalid quantity. ")
        if not item.get("product", {}).get("imageUrls"):
            return err(400, 25014, "The eBay listing associated with the inventory item, or the unpublished offer has invalid pictures. ")
        if not item.get("condition"):
            return err(400, 25021, "The eBay listing associated with the inventory item, or the unpublished offer has invalid item condition information. ")
        if CONDITION_ENUM_IDS.get(item["condition"]) not in [i for i, _ in cat["conditions"]]:
            return err(400, 25021, "The eBay listing associated with the inventory item, or the unpublished offer has invalid item "
                                   "condition information. The provided condition id is invalid for the selected primary category id.")
        given = item.get("product", {}).get("aspects", {})
        for a in cat["aspects"]:
            name = a["localizedAspectName"]
            if a["aspectConstraint"]["aspectRequired"] and not given.get(name):
                return user_error(f"The item specific {name} is missing. Add {name} to this listing, enter a valid value, and then try again.",
                                  [{"name": "2", "value": name}])
        return None

    def _publish(self, o: Offer):
        bad = self._publish_problems(o)
        if bad:
            return bad
        if o.listingId is None:
            o.listingId = f"1100{next(self._ids):08d}"
        o.status = "PUBLISHED"
        return FakeResponse(200, {"listingId": o.listingId, "warnings": []})

    # --- Account (business policies) ---------------------------------------------------------
    def _account(self, method, path, q, body):
        if path == "/program/get_opted_in_programs":
            return FakeResponse(200, {"programs": [{"programType": "SELLING_POLICY_MANAGEMENT"}] if self.opted_in else []})
        if path == "/program/opt_in" and method == "POST":
            if (body or {}).get("programType") != "SELLING_POLICY_MANAGEMENT":
                return err(400, 20401, "Invalid input. programType", domain="API_ACCOUNT")
            if self.opted_in:
                return err(409, 25803, "programType already exists.", domain="API_ACCOUNT")
            self.opted_in = True
            return FakeResponse(200, {})
        m = re.fullmatch(r"/(fulfillment|payment|return)_policy", path)
        if m:
            kind = m.group(1)
            key = {"fulfillment": "fulfillmentPolicies", "payment": "paymentPolicies", "return": "returnPolicies"}[kind]
            if not self.opted_in:
                return err(400, 20403, "User is not opted in to business policies.", domain="API_ACCOUNT",
                           category="BUSINESS", long="Opt in to SELLING_POLICY_MANAGEMENT first.")
            if method == "GET":
                found = [p for p in self.policies[kind].values() if p["marketplaceId"] == q.get("marketplace_id")]
                return FakeResponse(200, {key: copy.deepcopy(found), "total": len(found)})
            if method == "POST":
                b = body or {}
                for k in ("name", "marketplaceId", "categoryTypes"):
                    if not b.get(k):
                        return err(400, 20401, f"Invalid input. {k}", domain="API_ACCOUNT")
                if kind == "return" and "returnsAccepted" not in b:
                    return err(400, 20401, "Invalid input. returnsAccepted", domain="API_ACCOUNT")
                if any(p["name"] == b["name"] for p in self.policies[kind].values()):
                    return err(400, 20400, f"Invalid request. A policy named {b['name']} already exists.", domain="API_ACCOUNT")
                pid = str(6_000_000_000 + next(self._ids))
                self.policies[kind][pid] = {**copy.deepcopy(b), f"{kind}PolicyId": pid}
                return FakeResponse(201, {**copy.deepcopy(b), f"{kind}PolicyId": pid, "warnings": []},
                                    {"Location": f"https://api.sandbox.ebay.com/sell/account/v1/{kind}_policy/{pid}"})
        return err(404, 0, f"mock has no route for {method} /sell/account/v1{path}")

    # --- Fulfillment ---------------------------------------------------------------------------
    def _fulfillment(self, method, path, q, url, body=None):
        m = re.fullmatch(r"/order/([^/]+)(/shipping_fulfillment(?:/([^/]+))?)?", path)
        if m:
            return self._order_route(method, m.group(1), m.group(2), m.group(3), body)
        if path != "/order" or method != "GET":
            return err(404, 0, f"mock has no route for {method} /sell/fulfillment/v1{path}")
        try:
            limit = int(q.get("limit", 50))
            offset = int(q.get("offset", 0))
        except ValueError:
            return err(400, 30700, "Invalid limit or offset.", domain="API_FULFILLMENT")
        if limit > 200 or limit < 1:
            return err(400, 30700, "Invalid limit. Maximum is 200.", domain="API_FULFILLMENT")
        orders = self.orders
        f = q.get("filter", "")
        if f:
            m = re.fullmatch(r"orderfulfillmentstatus:\{([A-Z_|]+)\}", f)
            if not m:
                return err(400, 30800, "Invalid filter.", domain="API_FULFILLMENT")
            want = set(m.group(1).split("|"))
            orders = [o for o in orders if o["orderFulfillmentStatus"] in want]
        page = orders[offset:offset + limit]
        base = "https://api.sandbox.ebay.com/sell/fulfillment/v1/order"
        out = {"href": url, "limit": limit, "offset": offset, "total": len(orders), "orders": copy.deepcopy(page)}
        if offset + limit < len(orders):
            nq = f"?limit={limit}&offset={offset + limit}" + (f"&filter={f}" if f else "")
            out["next"] = base + nq
        return FakeResponse(200, out)

    def _order_route(self, method, order_id, sub, fid, body):
        """getOrder, getShippingFulfillments, createShippingFulfillment, getShippingFulfillment. Error ids are the
        ones eBay's fulfillment_api spec lists for these calls; 409 for a line item already shipped is the mock's guess."""
        o = next((o for o in self.orders if o["orderId"] == order_id), None)
        if o is None:
            return err(400, 32100, f"Invalid Order Id: {order_id}", domain="API_FULFILLMENT")
        if not sub:
            return FakeResponse(200, copy.deepcopy(o)) if method == "GET" else err(404, 0, "no route", domain="API_FULFILLMENT")
        done = self.fulfillments.setdefault(order_id, [])
        if fid:
            f = next((f for f in done if f["fulfillmentId"] == fid), None)
            return FakeResponse(200, copy.deepcopy(f)) if f else err(404, 32110, f"Fulfillment {fid} not found", domain="API_FULFILLMENT")
        if method == "GET":
            return FakeResponse(200, {"fulfillments": copy.deepcopy(done), "total": len(done), "warnings": []})
        if method != "POST":
            return err(404, 0, f"mock has no route for {method} shipping_fulfillment", domain="API_FULFILLMENT")
        b = body or {}
        lines = b.get("lineItems") or []
        if not lines:
            return err(400, 32600, "Invalid input data", domain="API_FULFILLMENT", long="lineItems is required.")
        ids = [li.get("lineItemId") for li in lines]
        if len(set(ids)) != len(ids):
            return err(400, 32210, "Duplicate line item in the request", domain="API_FULFILLMENT")
        by_id = {li["lineItemId"]: li for li in o["lineItems"]}
        for li in lines:
            if li.get("lineItemId") not in by_id:
                return err(400, 32200, f"Invalid line item id: {li.get('lineItemId')}", domain="API_FULFILLMENT")
            qty = li.get("quantity", 1)
            if not isinstance(qty, int) or qty < 1:
                return err(400, 32600, "Invalid input data", domain="API_FULFILLMENT", long="quantity must be a whole number above 0.")
        carrier, tracking = b.get("shippingCarrierCode"), b.get("trackingNumber")
        if bool(carrier) != bool(tracking) or (tracking and not re.fullmatch(r"[A-Za-z0-9]+", tracking)):
            return err(400, 32300, "Invalid shipment tracking number or carrier", domain="API_FULFILLMENT")
        shipped = b.get("shippedDate") or "2026-11-08T09:00:00.000Z"
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{3})?Z", shipped):
            return err(400, 32500, "Invalid shipped date", domain="API_FULFILLMENT")
        if any(by_id[i]["lineItemFulfillmentStatus"] == "FULFILLED" for i in ids):
            return FakeResponse(409, {"errors": [{"errorId": 0, "domain": "API_FULFILLMENT", "category": "REQUEST",
                                                  "message": "Conflict: line item already fulfilled"}]})
        fid = tracking or str(999 + len(done))
        done.append({"fulfillmentId": fid, "lineItems": copy.deepcopy(lines), "shipmentTrackingNumber": tracking,
                     "shippingCarrierCode": carrier, "shippedDate": shipped})
        for i in ids:
            by_id[i]["lineItemFulfillmentStatus"] = "FULFILLED"
        states = {li["lineItemFulfillmentStatus"] for li in o["lineItems"]}
        o["orderFulfillmentStatus"] = "FULFILLED" if states == {"FULFILLED"} else "IN_PROGRESS"
        loc = f"https://api.sandbox.ebay.com/sell/fulfillment/v1/order/{order_id}/shipping_fulfillment/{fid}"
        return FakeResponse(201, {}, {"Location": loc})
