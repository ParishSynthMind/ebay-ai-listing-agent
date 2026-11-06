"""Checks a drafted listing against eBay's rules and the category's own item specifics BEFORE any call to eBay.

A listing with an error is never sent. The checks are the ones that make publishOffer fail (or quietly
hurt a listing): the 80-character title, every required aspect present, values from the allowed list where
the category only accepts its own values, one value where only one is allowed, value lengths, a valid
condition, a price inside the seller's floor and ceiling, a whole-number quantity, https picture links,
and no scripts, forms or off-eBay links in the description (eBay bans active content).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

TITLE_MAX = 80
ASPECT_NAME_MAX = 40
ASPECT_VALUE_MAX = 50  # Inventory API limit; a category's own aspectMaxLength can be lower
DESCRIPTION_MAX = 500_000
MAX_IMAGES = 24
CONDITIONS = [
    "NEW", "LIKE_NEW", "NEW_OTHER", "NEW_WITH_DEFECTS",
    "CERTIFIED_REFURBISHED", "EXCELLENT_REFURBISHED", "VERY_GOOD_REFURBISHED", "GOOD_REFURBISHED",
    "SELLER_REFURBISHED", "USED_EXCELLENT", "USED_VERY_GOOD", "USED_GOOD", "USED_ACCEPTABLE",
    "FOR_PARTS_OR_NOT_WORKING",
]
BANNED_HTML = [
    (re.compile(r"<\s*(script|iframe|object|embed|form|input|button|link|meta|base)\b", re.I), "active content tag"),
    (re.compile(r"\son[a-z]+\s*=", re.I), "JavaScript event attribute"),
    (re.compile(r"javascript\s*:", re.I), "javascript: link"),
    (re.compile(r"<\s*a\b[^>]*href", re.I), "a link (off-eBay links are not allowed)"),
    (re.compile(r"@import|expression\s*\(", re.I), "active CSS"),
]


@dataclass
class Problem:
    field: str
    message: str
    severity: str = "error"  # error stops the listing; warning is shown in the preview

    def __str__(self) -> str:
        return f"{self.severity}: {self.field}: {self.message}"


def required_aspects(aspects: list[dict]) -> list[dict]:
    return [a for a in aspects if a.get("aspectConstraint", {}).get("aspectRequired")]


def _allowed(a: dict) -> list[str]:
    return [v["localizedValue"] for v in a.get("aspectValues", [])]


def check_listing(listing: dict, aspects: list[dict], floor: float | None = None, ceiling: float | None = None) -> list[Problem]:
    p: list[Problem] = []
    title = listing.get("title") or ""
    if not title.strip():
        p.append(Problem("title", "missing"))
    elif len(title) > TITLE_MAX:
        p.append(Problem("title", f"{len(title)} characters, eBay allows {TITLE_MAX}"))
    if title and title != title.strip():
        p.append(Problem("title", "starts or ends with a space", "warning"))

    given = {k: v for k, v in (listing.get("aspects") or {}).items()}
    by_name = {a["localizedAspectName"]: a for a in aspects}
    for a in required_aspects(aspects):
        name = a["localizedAspectName"]
        vals = [v for v in given.get(name, []) if str(v).strip()]
        if not vals:
            p.append(Problem(f"aspects.{name}", "required by this category but missing"))
    for name, vals in given.items():
        if len(name) > ASPECT_NAME_MAX:
            p.append(Problem(f"aspects.{name}", f"name longer than {ASPECT_NAME_MAX} characters"))
        if not isinstance(vals, list):
            p.append(Problem(f"aspects.{name}", "values must be a list"))
            continue
        a = by_name.get(name)
        c = (a or {}).get("aspectConstraint", {})
        limit = min(c.get("aspectMaxLength") or ASPECT_VALUE_MAX, ASPECT_VALUE_MAX)
        for v in vals:
            if len(str(v)) > limit:
                p.append(Problem(f"aspects.{name}", f"value {str(v)[:30]!r}... longer than {limit} characters"))
        if a is None:
            p.append(Problem(f"aspects.{name}", "not an item specific of this category", "warning"))
            continue
        if c.get("itemToAspectCardinality") == "SINGLE" and len(vals) > 1:
            p.append(Problem(f"aspects.{name}", f"only one value allowed, got {len(vals)}"))
        if c.get("aspectMode") == "SELECTION_ONLY":
            allowed = _allowed(a)
            bad = [v for v in vals if v not in allowed]
            if bad:
                p.append(Problem(f"aspects.{name}", f"{bad[0]!r} is not one of this category's values"))

    if listing.get("condition") not in CONDITIONS:
        p.append(Problem("condition", f"{listing.get('condition')!r} is not an eBay condition"))

    price = listing.get("price")
    if not isinstance(price, (int, float)) or isinstance(price, bool) or price <= 0:
        p.append(Problem("price", f"must be a positive number, got {price!r}"))
    else:
        if round(price, 2) != price:
            p.append(Problem("price", "more than two decimal places"))
        if floor is not None and price < floor:
            p.append(Problem("price", f"{price:.2f} is below the floor {floor:.2f}"))
        if ceiling is not None and price > ceiling:
            p.append(Problem("price", f"{price:.2f} is above the ceiling {ceiling:.2f}"))
    if not re.fullmatch(r"[A-Z]{3}", str(listing.get("currency") or "")):
        p.append(Problem("currency", "must be a three-letter code"))

    qty = listing.get("quantity")
    if not isinstance(qty, int) or isinstance(qty, bool) or qty < 1:
        p.append(Problem("quantity", f"must be a whole number of 1 or more, got {qty!r}"))

    images = listing.get("image_urls") or []
    if not images:
        p.append(Problem("image_urls", "eBay needs at least one picture link to publish"))
    if len(images) > MAX_IMAGES:
        p.append(Problem("image_urls", f"{len(images)} pictures, eBay allows {MAX_IMAGES}"))
    for u in images:
        if not str(u).startswith("https://"):
            p.append(Problem("image_urls", f"{str(u)[:60]!r} is not an https link"))

    desc = listing.get("description_html") or ""
    if not desc.strip():
        p.append(Problem("description_html", "missing"))
    if len(desc) > DESCRIPTION_MAX:
        p.append(Problem("description_html", "too long"))
    for rx, what in BANNED_HTML:
        if rx.search(desc):
            p.append(Problem("description_html", f"contains {what}"))
    if not listing.get("category_id"):
        p.append(Problem("category_id", "missing"))
    return p


def errors(problems: list[Problem]) -> list[Problem]:
    return [x for x in problems if x.severity == "error"]
