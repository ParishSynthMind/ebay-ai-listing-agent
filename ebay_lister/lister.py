"""The whole flow for one product: photos + note (or a CSV row) -> category -> item specifics -> AI draft ->
validation -> inventory item -> offer -> publish. Nothing is sent to eBay unless validation passes.

Re-running is safe: the SKU is stable, the inventory item is a PUT (create or replace), an existing offer for
the SKU is updated instead of a second one being made, and an already published offer is revised in place.

A draft the tool is not sure of (the model says low confidence, a policy wording warning, a wide price band, a
category from the keyword map, or a seller note that tries to instruct the AI) is held as needs_review and only
sent once a person runs `ebay-lister approve SKU`.
"""
from __future__ import annotations

import csv
import hashlib
import html
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional

from .api import MARKETPLACE, Account, Fulfillment, Inventory, Taxonomy
from .http import EbayApiError
from .llm import Drafter, load_image
from .policy import check_wording, note_has_instructions
from .repricing import PriceDecision, decide, last_sold
from .sku import make_sku
from .store import Store
from .validate import CONDITIONS, TITLE_MAX, Problem, check_listing, errors

ALLOWED_TAGS = {"p", "ul", "ol", "li", "b", "strong", "i", "em", "br", "h3", "h4", "table", "tr", "td", "th", "tbody"}
DANGLING = {"with", "and", "for", "the", "a", "an", "of", "in", "on", "to", "or", "by", "plus", "&", "+"}
STOP = {"the", "a", "an", "and", "for", "with", "of", "in", "on", "mens", "men", "womens", "women", "used", "new"}


@dataclass
class ItemInput:
    sku: str
    note: str = ""
    photos: list[Path] = field(default_factory=list)
    image_urls: list[str] = field(default_factory=list)
    quantity: int = 1
    price: Optional[float] = None
    floor: Optional[float] = None
    ceiling: Optional[float] = None
    category_id: str = ""
    condition: str = ""
    currency: str = "USD"


@dataclass
class Draft:
    item: ItemInput
    status: str                      # ready | needs_review | needs_fix | needs_category | error
    listing: dict = field(default_factory=dict)
    problems: list[Problem] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    review: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"sku": self.item.sku, "status": self.status, "listing": self.listing,
                "problems": [str(p) for p in self.problems], "review": self.review, "notes": self.notes}


@dataclass
class Setup:
    """Seller-side ids every offer needs, written by `ebay-lister setup-sandbox`."""
    merchant_location_key: str
    fulfillment_policy_id: str
    payment_policy_id: str
    return_policy_id: str
    marketplace: str = MARKETPLACE

    @classmethod
    def load(cls, path: Path) -> Optional["Setup"]:
        return cls(**json.loads(Path(path).read_text())) if Path(path).exists() else None

    def save(self, path: Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(self.__dict__, indent=2))


# --- reading the input ------------------------------------------------------------
def _num(v) -> Optional[float]:
    v = (v or "").strip().replace("$", "").replace(",", "")
    return float(v) if v else None


def read_csv(path: Path) -> list[ItemInput]:
    """Columns: sku, note, photos (; separated, relative to the CSV), image_urls (;), quantity, price, floor,
    ceiling, category_id, condition, currency. Only note (or photos) is needed."""
    base = Path(path).parent
    out = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            if not (row.get("note") or row.get("photos")):
                continue
            photos = [base / p.strip() for p in row.get("photos", "").split(";") if p.strip()]
            out.append(ItemInput(
                sku=make_sku(row), note=row.get("note", ""), photos=photos,
                image_urls=[u.strip() for u in row.get("image_urls", "").split(";") if u.strip()],
                quantity=int(row.get("quantity") or 1), price=_num(row.get("price")), floor=_num(row.get("floor")),
                ceiling=_num(row.get("ceiling")), category_id=row.get("category_id", ""),
                condition=row.get("condition", "").upper(), currency=(row.get("currency") or "USD").upper()))
    return out


# --- cleaning what the model wrote ---------------------------------------------------
class _Clean(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "iframe", "object"):
            self.skip += 1
        elif tag in ALLOWED_TAGS and not self.skip:
            self.out.append(f"<{tag}>")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "iframe", "object"):
            self.skip = max(0, self.skip - 1)
        elif tag in ALLOWED_TAGS and tag != "br" and not self.skip:
            self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(html.escape(data, quote=False))


def clean_html(s: str) -> str:
    """Keep only simple formatting tags, with no attributes; drop scripts, styles, links and images."""
    c = _Clean()
    c.feed(s or "")
    c.close()
    return "".join(c.out).strip()


def fit_title(title: str) -> tuple[str, bool]:
    t = " ".join((title or "").split())
    if len(t) <= TITLE_MAX:
        return t, False
    cut = t[:TITLE_MAX + 1].rsplit(" ", 1)[0].rstrip(" ,-/|&+")
    words = cut.split(" ")
    while len(words) > 1 and words[-1].lower() in DANGLING:  # "... Black with" reads as broken
        words.pop()
    return " ".join(words).rstrip(" ,-/|&+")[:TITLE_MAX], True


def plain_text(desc_html: str, limit: int = 4000) -> str:
    t = re.sub(r"<[^>]+>", " ", desc_html)
    return " ".join(html.unescape(t).split())[:limit]


def _stem(w: str) -> str:
    for end, repl in (("ies", "y"), ("es", ""), ("s", "")):
        if w.endswith(end) and len(w) > len(end) + 2:
            return w[: -len(end)] + repl
    return w


def _words(s: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in STOP and len(w) > 1}


def input_hash(item: ItemInput) -> str:
    """What the draft was made from. Same input = reuse the saved draft (no AI call, and what was previewed is what is listed)."""
    h = hashlib.sha256()
    for part in (item.note, item.image_urls, item.quantity, item.price, item.floor, item.ceiling, item.category_id,
                 item.condition, item.currency):
        h.update(repr(part).encode() + b"\0")
    for p in item.photos:
        h.update(Path(p).read_bytes() if Path(p).exists() else repr(p).encode())
    return h.hexdigest()[:16]


# The model does not know the price when its high is more than twice its low AND the gap is real money
# (a 5.99 to 12.99 band on a mug is normal; 60 to 160 on a camera is a guess).
PRICE_BAND_MAX_RATIO = 2.0
PRICE_BAND_MIN_GAP = 10.0


def review_reasons(item: ItemInput, listing: dict, problems: list[Problem]) -> list[str]:
    """Why a person should look at this draft before it goes live. Empty = the tool is sure enough."""
    out = [f"wording: {p.message}" for p in problems if p.severity == "warning" and p.field.startswith("policy.")]
    ai = listing.get("ai") or {}
    if ai.get("confidence") == "low":
        out.append("the AI says it is not sure of this item")
    lo, hi = ai.get("price_low"), ai.get("price_high")
    if ai.get("price_from") == "ai" and isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and lo > 0 \
            and hi / lo > PRICE_BAND_MAX_RATIO and hi - lo > PRICE_BAND_MIN_GAP:
        out.append(f"wide price band {lo:g} to {hi:g}: the AI does not know the price well")
    if listing.get("category_source") == "keyword_map":
        out.append(f"category {listing.get('category_id')} came from the keyword map, not eBay: confirm it")
    if note_has_instructions(item.note):
        out.append("the seller note tries to give the AI instructions: check nothing in the draft followed them")
    return out


# --- the lister -------------------------------------------------------------------------
class Lister:
    def __init__(self, taxonomy: Taxonomy, inventory: Inventory, drafter: Optional[Drafter], store: Store,
                 setup: Optional[Setup] = None, category_map: Optional[dict] = None, max_publish: Optional[int] = None):
        self.taxonomy = taxonomy
        self.inventory = inventory
        self.drafter = drafter
        self.store = store
        self.setup = setup
        self.category_map = {k.lower(): str(v) for k, v in (category_map or {}).items()}
        self.max_publish = max_publish  # new listings per run (each can cost an insertion fee in production)
        self.published = 0

    def choose_category(self, query: str, product: str) -> tuple[Optional[dict], list[str]]:
        """eBay's suggestion if it makes sense for the item, else the keyword map, else None (a person picks).

        The sanity check matters: eBay documents that getCategorySuggestions is not supported in the sandbox
        and can return boilerplate names there, so a suggestion sharing no word with the item is not trusted."""
        notes = []
        want = _words(query) | _words(product)
        mapped = self._mapped(query, product)
        try:
            for s in self.taxonomy.suggest(query):
                if want & _words(s["path"]) or (mapped and mapped[1] == s["categoryId"]):
                    return s, notes
                notes.append(f"ignored eBay suggestion {s['categoryName']!r} ({s['categoryId']}): no word in common with the item")
        except EbayApiError as e:
            notes.append(f"category suggestions failed: {e}")
        if mapped:
            kw, cid = mapped
            notes.append(f"category {cid} from the keyword map ({kw!r})")
            return {"categoryId": cid, "categoryName": f"category {cid}", "path": f"category {cid} (keyword map)",
                    "source": "keyword_map"}, notes
        return None, notes

    def _mapped(self, query: str, product: str) -> Optional[tuple[str, str]]:
        text = (query + " " + product).lower()
        words = _words(text)
        for kw, cid in self.category_map.items():
            if _words(kw) <= words or kw in text:
                return kw, cid
        return None

    def draft(self, item: ItemInput, redraft: bool = False) -> Draft:
        d = Draft(item, "error")
        ih = input_hash(item)
        saved = self.store.get(item.sku)
        if not redraft and saved and saved.get("input_hash") == ih and saved.get("draft") not in (None, "{}") \
                and saved["status"] not in ("error", "needs_category"):
            d.listing = json.loads(saved["draft"])
            aspects = self.taxonomy.aspects(d.listing["category_id"])
            self._judge(d, aspects, ih)
            d.notes.append("same input as last time: reused the saved draft, no AI call (--redraft to write it again)")
            if saved["status"] in ("needs_review", "needs_fix", "ready") and d.status != saved["status"]:
                self.store.save(item.sku, d.status, d.listing, d.problems)
            return d
        try:
            images = [load_image(p) for p in item.photos]
            if self.drafter is None:
                raise RuntimeError("no AI provider configured")
            if item.category_id:
                category = {"categoryId": item.category_id, "categoryName": f"category {item.category_id}",
                            "path": f"category {item.category_id} (from the CSV)", "source": "csv"}
                product = item.note
            else:
                ident = self.drafter.identify(item.note, images)
                product = ident.get("product", "")
                category, notes = self.choose_category(ident.get("search_query", ""), product)
                d.notes += notes
                if category is None:
                    d.status = "needs_category"
                    d.notes.append(f"no category found for {product!r}: add category_id to the row")
                    self.store.save(item.sku, d.status, {}, [])
                    self.store.event(item.sku, "draft:needs_category", product)
                    return d
            aspects = self.taxonomy.aspects(category["categoryId"])
            raw = self.drafter.draft(item.note, images, category, aspects, item.currency)
            d.listing = self.build_listing(item, category, raw, d.notes)
            self._judge(d, aspects, ih)
        except EbayApiError as e:
            d.status = "error"
            d.notes.append(str(e))
        except (RuntimeError, ValueError, OSError) as e:
            d.status = "error"
            d.notes.append(f"{type(e).__name__}: {e}")
        live = saved and saved["status"] in ("published", "revised", "offer_ready")
        self.store.save(item.sku, saved["status"] if live and d.status == "ready" else d.status, d.listing, d.problems,
                        input_hash=ih if d.status in ("ready", "needs_review", "needs_fix") else None)
        self.store.event(item.sku, f"draft:{d.status}", "; ".join(map(str, d.problems))[:500])
        return d

    def _judge(self, d: Draft, aspects: list[dict], ih: str) -> None:
        """Validation + policy wording, then: errors -> needs_fix; unsure -> needs_review unless a person approved
        this exact input; otherwise ready."""
        d.problems = check_listing(d.listing, aspects, d.item.floor, d.item.ceiling) + check_wording(d.listing)
        d.review = review_reasons(d.item, d.listing, d.problems)
        if errors(d.problems):
            d.status = "needs_fix"
        elif d.review and not self.store.approved(d.item.sku, ih):
            d.status = "needs_review"
        else:
            d.status = "ready"
            if d.review:
                d.notes.append("approved by a person for this input")

    def build_listing(self, item: ItemInput, category: dict, raw: dict, notes: list[str]) -> dict:
        title, cut = fit_title(raw.get("title", ""))
        if cut:
            notes.append(f"title shortened to {TITLE_MAX} characters at a word break")
        aspects: dict[str, list[str]] = {}
        for a in raw.get("aspects", []):
            vals = [" ".join(str(v).split()) for v in a.get("values", []) if str(v).strip()]
            name = " ".join(str(a.get("name", "")).split())
            if name and vals:
                aspects.setdefault(name, [])
                aspects[name] += [v for v in vals if v not in aspects[name]]
        price_ai = raw.get("price") or {}
        price = item.price if item.price is not None else price_ai.get("value")
        if isinstance(price, (int, float)):
            price = round(float(price), 2)
            if item.floor is not None and price < item.floor:
                notes.append(f"AI price {price:.2f} raised to the floor {item.floor:.2f}")
                price = item.floor
            if item.ceiling is not None and price > item.ceiling:
                notes.append(f"AI price {price:.2f} lowered to the ceiling {item.ceiling:.2f}")
                price = item.ceiling
        condition = item.condition if item.condition in CONDITIONS else raw.get("condition")
        return {
            "sku": item.sku, "title": title, "category_id": category["categoryId"], "category_path": category.get("path", ""),
            "category_source": category.get("source", "ebay"),
            "condition": condition, "condition_description": (raw.get("condition_description") or "").strip()[:1000],
            "aspects": aspects, "description_html": clean_html(raw.get("description_html", "")),
            "price": price, "currency": item.currency, "quantity": item.quantity, "image_urls": item.image_urls,
            "ai": {"price_reasoning": price_ai.get("reasoning", ""), "price_low": price_ai.get("low"),
                   "price_high": price_ai.get("high"), "check": raw.get("check", []),
                   "confidence": raw.get("confidence", "medium"),
                   "price_from": "csv" if item.price is not None else "ai",
                   "provider": getattr(self.drafter, "provider", ""), "model": getattr(self.drafter, "model", "")},
        }

    # --- sending to eBay ----------------------------------------------------------------
    def item_body(self, L: dict) -> dict:
        body = {
            "availability": {"shipToLocationAvailability": {"quantity": L["quantity"]}},
            "condition": L["condition"],
            "product": {"title": L["title"], "description": plain_text(L["description_html"]),
                        "aspects": L["aspects"], "imageUrls": L["image_urls"]},
        }
        if L.get("condition_description") and not str(L["condition"]).startswith("NEW"):
            body["conditionDescription"] = L["condition_description"]
        return body

    def offer_body(self, L: dict) -> dict:
        s = self.setup
        return {
            "sku": L["sku"], "marketplaceId": s.marketplace, "format": "FIXED_PRICE",
            "availableQuantity": L["quantity"], "categoryId": L["category_id"],
            "listingDescription": L["description_html"], "merchantLocationKey": s.merchant_location_key,
            "listingPolicies": {"fulfillmentPolicyId": s.fulfillment_policy_id, "paymentPolicyId": s.payment_policy_id,
                                "returnPolicyId": s.return_policy_id},
            "pricingSummary": {"price": {"value": f"{L['price']:.2f}", "currency": L["currency"]}},
        }

    def send(self, d: Draft, publish: bool = True) -> dict:
        """Returns {"sku", "status", "offerId", "listingId", "error"}. Only drafts with no errors are sent."""
        sku = d.item.sku
        res = {"sku": sku, "status": "skipped", "offerId": None, "listingId": None, "error": ""}
        if d.status != "ready" or errors(d.problems):
            res["error"] = f"not sent: draft is {d.status}"
            return res
        if self.setup is None:
            raise RuntimeError("no seller setup yet: run `ebay-lister setup-sandbox` first")
        L = d.listing
        try:
            self.inventory.put_item(sku, self.item_body(L))
            offer = self.offer_body(L)
            existing = self.inventory.offers_for(sku, self.setup.marketplace)
            if existing:
                oid = existing[0]["offerId"]
                live_price = (existing[0].get("pricingSummary") or {}).get("price")
                if live_price and d.item.price is None:
                    # the price on eBay may have been set by the repricer since; only a price in the CSV overrides it
                    offer["pricingSummary"]["price"] = live_price
                    L = {**L, "price": float(live_price["value"])}
                self.inventory.update_offer(oid, offer)
                published = existing[0].get("status") == "PUBLISHED"
                listing_id = (existing[0].get("listing") or {}).get("listingId")
            else:
                oid, published, listing_id = self._create_offer(offer), False, None
            res["offerId"] = oid
            was_live = published
            if publish and not published and self.max_publish is not None and self.published >= self.max_publish:
                res.update(status="offer_ready", error=f"publish cap of {self.max_publish} new listings this run reached; "
                                                       "offer saved, run again to publish")
                self.store.save(sku, "offer_ready", L, d.problems, oid)
                self.store.event(sku, "publish_cap", res["error"])
                return res
            if publish and not published:
                listing_id = self.inventory.publish_offer(oid)
                published = True
                self.published += 1
            res.update(listingId=listing_id, status="revised" if was_live else "published" if published else "offer_ready")
            self.store.save(sku, res["status"], L, d.problems, oid, listing_id)
            self.store.event(sku, res["status"], f"offer {oid} listing {listing_id}")
        except EbayApiError as e:
            res.update(status="ebay_error", error="; ".join(f"{i}: {m}" for i, m in zip(e.error_ids, e.messages())))
            self.store.save(sku, "ebay_error", L, d.problems)
            self.store.event(sku, "ebay_error", res["error"][:500])
        return res

    def _create_offer(self, offer: dict) -> str:
        try:
            return self.inventory.create_offer(offer)
        except EbayApiError as e:
            # Another run made the offer between our check and our create: eBay names it, so update that one.
            for er in e.errors:
                if "already exists" in str(er.get("message", "")):
                    oid = next((p["value"] for p in er.get("parameters", []) if p.get("name") == "offerId"), None)
                    if oid:
                        self.inventory.update_offer(oid, offer)
                        return oid
            raise


# --- seller setup (sandbox) ---------------------------------------------------------------
def setup_sandbox(account: Account, inventory: Inventory, cfg: dict, create: bool = True) -> tuple[Setup, list[str]]:
    """Opt in to business policies, make one shipping, payment and return policy and one location if missing.

    create=False (production): only find the seller's existing policies and location by the names in the config,
    never make or change anything on a real account."""
    said = []
    if not create:
        return _find_existing(account, inventory, cfg)
    if "SELLING_POLICY_MANAGEMENT" not in account.opted_in():
        account.opt_in_policies()
        said.append("opted in to business policies (eBay says this can take a while to apply)")
    mk = cfg.get("marketplace", MARKETPLACE)
    ids = {}
    for kind in ("fulfillment", "payment", "return"):
        name = cfg["policies"][kind]["name"]
        found = [p for p in account.policies(kind) if p.get("name") == name]
        if found:
            ids[kind] = found[0][f"{kind}PolicyId"]
            said.append(f"{kind} policy {name!r} already there ({ids[kind]})")
        else:
            body = {**cfg["policies"][kind], "marketplaceId": mk}
            ids[kind] = account.create_policy(kind, body)
            said.append(f"made {kind} policy {name!r} ({ids[kind]})")
    key = cfg["location"]["key"]
    if inventory.get_location(key) is None:
        inventory.create_location(key, cfg["location"]["body"])
        said.append(f"made inventory location {key!r}")
    else:
        said.append(f"inventory location {key!r} already there")
    return Setup(key, ids["fulfillment"], ids["payment"], ids["return"], mk), said


def _find_existing(account: Account, inventory: Inventory, cfg: dict) -> tuple[Setup, list[str]]:
    said, ids, missing = [], {}, []
    if "SELLING_POLICY_MANAGEMENT" not in account.opted_in():
        raise ValueError("production setup never creates anything, and this seller account is not opted in to "
                         "business policies: opt in to business policies on the seller account and make them first")
    for kind in ("fulfillment", "payment", "return"):
        name = cfg["policies"][kind]["name"]
        found = [p for p in account.policies(kind) if p.get("name") == name]
        if found:
            ids[kind] = found[0][f"{kind}PolicyId"]
            said.append(f"using {kind} policy {name!r} ({ids[kind]})")
        else:
            missing.append(f"{kind} policy named {name!r}")
    key = cfg["location"]["key"]
    if inventory.get_location(key) is None:
        missing.append(f"inventory location {key!r}")
    if missing:
        raise ValueError("production setup never creates anything; these are missing on the seller account: "
                         + ", ".join(missing) + ". Put your own policy names in config/lister.yaml")
    return Setup(key, ids["fulfillment"], ids["payment"], ids["return"], cfg.get("marketplace", MARKETPLACE)), said


# --- marking orders shipped -------------------------------------------------------------------
def clean_tracking(tracking: str) -> str:
    """eBay only accepts letters and digits: spaces and hyphens from a label are removed, anything else is refused."""
    t = re.sub(r"[\s-]", "", tracking or "")
    if not re.fullmatch(r"[A-Za-z0-9]{4,40}", t):
        raise ValueError(f"tracking number {tracking!r} must be letters and digits only")
    return t


def mark_shipped(fulfillment: Fulfillment, store: Store, order_id: str, carrier: str, tracking: str,
                 line_item_ids: Optional[list[str]] = None, shipped_date: Optional[str] = None, apply: bool = True) -> dict:
    """Mark an order's line items shipped with tracking. Safe to run twice: a fulfillment with the same tracking
    number, or line items already shipped, are reported as already_shipped and nothing is sent."""
    res = {"order_id": order_id, "status": "", "fulfillment_id": "", "line_items": [], "error": ""}
    try:
        t = clean_tracking(tracking)
        carrier = (carrier or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{2,40}", carrier):
            raise ValueError(f"carrier {carrier!r} must be an eBay carrier code such as USPS, UPS or FedEx")
        if shipped_date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{3})?Z", shipped_date):
            raise ValueError("shipped date must be UTC ISO 8601, e.g. 2026-11-08T09:30:00.000Z")
    except ValueError as e:
        res.update(status="invalid", error=str(e))
        return res
    try:
        order = fulfillment.get_order(order_id)
        lines = {li["lineItemId"]: li for li in order.get("lineItems", [])}
        wanted = line_item_ids or list(lines)
        unknown = [i for i in wanted if i not in lines]
        if unknown:
            res.update(status="invalid", error=f"line item {unknown[0]} is not in order {order_id}")
            return res
        res["line_items"] = wanted
        for f in fulfillment.shipping_fulfillments(order_id):
            if (f.get("shipmentTrackingNumber") or f.get("fulfillmentId")) == t:
                res.update(status="already_shipped", fulfillment_id=f["fulfillmentId"])
                return res
        todo = [i for i in wanted if lines[i].get("lineItemFulfillmentStatus") != "FULFILLED"]
        if not todo:
            res.update(status="already_shipped", error="every line item is already marked shipped")
            return res
        body = {"lineItems": [{"lineItemId": i, "quantity": int(lines[i].get("quantity", 1))} for i in todo],
                "shippingCarrierCode": carrier, "trackingNumber": t}
        if shipped_date:
            body["shippedDate"] = shipped_date
        res["line_items"] = todo
        if not apply:
            res.update(status="dry_run", body=body)
            return res
        fid = fulfillment.create_shipping_fulfillment(order_id, body)
        res.update(status="shipped", fulfillment_id=fid)
        store.shipped(order_id, t, carrier, fid)
        for i in todo:
            store.event(lines[i].get("sku") or "", "shipped", f"order {order_id} {carrier} {t}")
    except EbayApiError as e:
        res.update(status="ebay_error", error="; ".join(f"{i}: {m}" for i, m in zip(e.error_ids, e.messages())))
    return res


def read_shipments(path: Path) -> list[dict]:
    """Columns: order_id, carrier, tracking, and optional line_item_id (; separated) and shipped_date."""
    out = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            if row.get("order_id"):
                out.append({"order_id": row["order_id"], "carrier": row.get("carrier", ""), "tracking": row.get("tracking", ""),
                            "line_item_ids": [i for i in row.get("line_item_id", "").split(";") if i] or None,
                            "shipped_date": row.get("shipped_date") or None})
    return out


# --- repricing ----------------------------------------------------------------------------------
def reprice(inventory: Inventory, fulfillment: Fulfillment, store: Store, rules: dict,
            competitors: Optional[dict] = None, apply: bool = False, now=None) -> list[PriceDecision]:
    from datetime import datetime

    sold = last_sold(fulfillment.orders())
    out = []
    for row in store.all():
        if not row.get("offer_id"):
            continue
        offers = inventory.offers_for(row["sku"])
        if not offers:
            continue
        offer = offers[0]
        current = float(offer["pricingSummary"]["price"]["value"])
        listed = datetime.fromisoformat(row["first_listed"].replace("Z", "+00:00")) if row.get("first_listed") else None
        d = decide(row["sku"], current, rules, (competitors or {}).get(row["sku"]), sold.get(row["sku"]), listed, now)
        if apply and d.changed:
            floor = (rules.get("skus") or {}).get(row["sku"], {}).get("floor", (rules.get("defaults") or {}).get("floor"))
            if floor is None or d.new < float(floor):
                raise AssertionError("refusing to send a price below the floor")
            body = {k: v for k, v in offer.items() if k not in ("offerId", "status", "listing")}
            body["pricingSummary"] = {**body.get("pricingSummary", {}),
                                      "price": {"value": f"{d.new:.2f}", "currency": offer["pricingSummary"]["price"]["currency"]}}
            inventory.update_offer(offer["offerId"], body)
            store.set_price(row["sku"], d.new)
            store.event(row["sku"], "repriced", f"{d.old:.2f} -> {d.new:.2f}: {'; '.join(d.reasons)}")
        out.append(d)
    return out
