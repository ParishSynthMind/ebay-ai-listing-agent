"""Wording checks against eBay's listing policies, run on what the AI wrote BEFORE anything is sent.

The model is told the same rules, but a rule written in a prompt is a hope, not a check. This module is
the check. Each rule names the eBay policy it comes from; docs/EBAY-FACTS.md says which policy pages were
read on the day and which rules come from eBay's policies as commonly quoted.

- error: the listing is not sent (counterfeit words, contact details, payment outside eBay, question marks
  in the title or item specifics, comparisons with or names of other brands in the title).
- warning: the listing goes to a person for review (shouting, punctuation spam, seller endorsement claims,
  promoting other listings, comparison wording in the description).
"""
from __future__ import annotations

import html
import re

from .validate import Problem

# Search manipulation policy (read on ebay.com, 8 Nov 2026): keywords must be about the item, no comparisons
# with other products, no promoting other listings, no keywords with question marks.
# Explicit comparison phrases stop the listing; "like X" / "not X" with a capitalised name only goes to review,
# because Title Case titles make them ambiguous ("Not Original Box").
COMPARISON_CI = re.compile(r"\b(similar to|same as|compare(?:d|s)? to|comparable to|better than|alternative to|"
                           r"dupe|versus|vs\.?)(?!\w)", re.I)
COMPARISON_NAME = re.compile(r"\b(?:[Ll]ike|[Nn]ot) (?!New\b|new\b|Working\b|Tested\b|Included\b|Sure\b|A\b)[A-Z][\w&'-]+")
PROMOTE = re.compile(r"\b(see|check out|visit|browse) (all )?(my|our) (other )?(items|listings|store|shop|auctions)\b", re.I)
# Third-party endorsements policy (read on ebay.com, 8 Nov 2026): claims buyers cannot check are not allowed.
ENDORSE = re.compile(r"\b(trusted seller|reliable seller|buy with confidence|100% (safe|genuine seller)|safe seller)\b", re.I)
# Counterfeit / replica policy (eBay policy as commonly quoted; page not readable from here on the day).
COUNTERFEIT = re.compile(r"\b(replica|counterfeit|knock[- ]?off|bootleg|unauthori[sz]ed copy|inspired by|"
                         r"designer[- ]inspired|1:1 copy|mirror quality|aaa quality)\b", re.I)
FAKE = re.compile(r"\bfake\b", re.I)
# Offers to buy or sell outside eBay, links policy (as commonly quoted; pages not readable on the day).
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE = re.compile(r"(?<![\w.])(?:\+?\d[\d ().-]{8,}\d)(?![\w.])")
URL = re.compile(r"\b(https?://|www\.)\S+|\b[\w-]+\.(com|net|org|co\.uk|shop|store|io)\b", re.I)
OFF_EBAY = re.compile(r"\b(venmo|zelle|cash ?app|western union|wire transfer|bank transfer|paypal (friends|f&f|gift)|"
                      r"pay(ment)? (outside|off) (of )?ebay|contact me (directly|off ebay|outside ebay)|text me|"
                      r"whats ?app me|call me on|e-?mail me)\b", re.I)
# Keyword spam: the thresholds are this tool's own, the rule behind them is the search manipulation policy.
SHOUT = re.compile(r"\b(l@@k|wow+|must see|look+|hot+|rare!+|nr|no reserve)\b|[!?*@#]{2,}", re.I)

# Brands this tool knows well enough to say "that is another brand". Small on purpose: a false error
# stops a real listing, so only names that are never ordinary words are here.
BRANDS = ["Nike", "Adidas", "Puma", "Reebok", "Asics", "New Balance", "Converse", "Vans", "Gucci", "Prada",
          "Louis Vuitton", "Chanel", "Rolex", "Omega", "Canon", "Nikon", "Sony", "Fujifilm", "Olympus", "Pentax",
          "Leica", "Apple", "Samsung", "Dyson", "Lego", "Le Creuset", "Yeti", "Stanley", "Patagonia", "Supreme"]

NOTE_INSTRUCTIONS = re.compile(r"\b(ignore (all |any |your |the |previous |prior )*(rules|instructions|prompt)|"
                               r"system (note|prompt|message)|you are now|as an ai|disregard (the|your))\b", re.I)


def _text(s: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", s or "")).split())


def _brand_rx(b: str) -> re.Pattern:
    return re.compile(r"(?<![\w-])" + re.escape(b).replace(r"\ ", r"[\s-]+") + r"(?![\w-])", re.I)


def other_brands(title: str, own: list[str]) -> list[str]:
    own_l = {o.lower() for o in own}
    return [b for b in BRANDS if b.lower() not in own_l and _brand_rx(b).search(title)]


def check_wording(listing: dict) -> list[Problem]:
    p: list[Problem] = []
    title = listing.get("title") or ""
    desc = _text(listing.get("description_html") or "")
    cond = listing.get("condition_description") or ""
    aspects = listing.get("aspects") or {}
    aspect_text = " | ".join(f"{k}: {' / '.join(map(str, v))}" for k, v in aspects.items())
    fields = {"title": title, "description_html": desc, "condition_description": cond, "aspects": aspect_text}

    for name, text in fields.items():
        if not text:
            continue
        m = COUNTERFEIT.search(text)
        if m:
            p.append(Problem(f"policy.{name}", f"{m.group(0)!r} reads as a replica or counterfeit (eBay counterfeit policy)"))
        m = FAKE.search(text)
        if m:
            p.append(Problem(f"policy.{name}", "'fake' reads as counterfeit; for materials say 'faux' (eBay counterfeit policy)", "warning"))
        rest = text
        for rx, what in ((EMAIL, "an email address"), (URL, "a web address"), (PHONE, "a phone number")):
            m = next((m for m in rx.finditer(rest) if rx is not PHONE or len(re.sub(r"\D", "", m.group(0))) >= 9), None)
            if m:
                p.append(Problem(f"policy.{name}", f"contains {what} ({m.group(0)[:30]!r}); eBay listings cannot send buyers off eBay"))
            rest = rx.sub(" ", rest)
        m = OFF_EBAY.search(text)
        if m:
            p.append(Problem(f"policy.{name}", f"{m.group(0)!r}: payment or contact outside eBay is not allowed"))
        m = ENDORSE.search(text)
        if m:
            p.append(Problem(f"policy.{name}", f"{m.group(0)!r} is an endorsement claim buyers cannot check (eBay third-party endorsements policy)", "warning"))
        m = PROMOTE.search(text)
        if m:
            p.append(Problem(f"policy.{name}", f"{m.group(0)!r} promotes other listings (eBay search manipulation policy)", "warning"))

    for name in ("title", "aspects"):
        if "?" in fields[name]:
            p.append(Problem(f"policy.{name}", "contains a question mark; eBay does not allow keywords with question marks. "
                                               "Leave out what is not known (eBay search manipulation policy)"))
    m = COMPARISON_CI.search(title)
    if m:
        p.append(Problem("policy.title", f"{m.group(0)!r} compares with another product (eBay search manipulation policy)"))
    m = COMPARISON_NAME.search(title)
    if m:
        p.append(Problem("policy.title", f"{m.group(0)!r} may compare with another product; check it", "warning"))
    m = COMPARISON_CI.search(desc)
    if m:
        p.append(Problem("policy.description_html", f"{m.group(0)!r} compares with another product (eBay search manipulation policy)", "warning"))
    own = [str(v) for v in aspects.get("Brand", [])]
    if own:
        extra = other_brands(title, own)
        if extra:
            p.append(Problem("policy.title", f"names {', '.join(extra)} but the item's brand is {', '.join(own)} "
                                             "(keywords must be about the item: eBay search manipulation policy)"))

    words = re.findall(r"[A-Za-z]{4,}", title)
    caps = [w for w in words if w.isupper()]
    if words and len(caps) >= 3 and len(caps) / len(words) > 0.4:
        p.append(Problem("policy.title", f"{len(caps)} words in capitals reads as shouting", "warning"))
    m = SHOUT.search(title)
    if m:
        p.append(Problem("policy.title", f"{m.group(0)!r} is attention-grabbing filler, not a search keyword", "warning"))
    seen: dict[str, int] = {}
    for w in re.findall(r"[a-z0-9]+", title.lower()):
        if len(w) > 2:
            seen[w] = seen.get(w, 0) + 1
    rep = [w for w, n in seen.items() if n >= 3]
    if rep:
        p.append(Problem("policy.title", f"{rep[0]!r} repeated {seen[rep[0]]} times reads as keyword stuffing", "warning"))
    return p


def note_has_instructions(note: str) -> bool:
    """A seller note that tries to give the AI orders. The model is told to ignore it; a person checks it anyway."""
    return bool(NOTE_INSTRUCTIONS.search(note or ""))
