"""ebay-lister: photos or a CSV in, validated eBay sandbox listings out.

  ebay-lister check                          tokens, category tree, seller setup
  ebay-lister consent-url                    the page where the sandbox seller says yes (once)
  ebay-lister exchange-code "<url or code>"  swap the code for a refresh token (saved to the Keychain)
  ebay-lister setup-sandbox                  business policies + an inventory location, if missing
  ebay-lister categories "running shoes"     eBay's category suggestions
  ebay-lister aspects 15709                  the item specifics a category wants
  ebay-lister draft --csv items.csv          AI drafts + validation, nothing sent (dry run)
  ebay-lister draft --photos dir --note ".." one item from photos
  ebay-lister list --csv items.csv           draft, validate, create/update and publish
  ebay-lister list --csv items.csv --no-publish   stop at an unpublished offer
  ebay-lister orders [--open]                orders from the Fulfillment API
  ebay-lister reprice [--competitors f.csv] [--apply]
  ebay-lister report
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

import yaml

from . import keychain
from .api import Account, Fulfillment, Inventory, Taxonomy
from .auth import EbayOAuth, base_urls
from .http import EbayApiError, EbayHttp
from .lister import ItemInput, Lister, Setup, read_csv, reprice, setup_sandbox
from .llm import CostCapReached, make_drafter
from .sku import make_sku
from .store import Store

ROOT = Path.cwd()


@dataclass
class Ctx:
    cfg: dict
    oauth: EbayOAuth
    taxonomy: Taxonomy
    inventory: Inventory
    account: Account
    fulfillment: Fulfillment
    store: Store
    state: Path
    app_http: EbayHttp
    user_http: EbayHttp

    @property
    def setup_path(self) -> Path:
        return self.state / "seller-setup.json"


def load_config(path: Optional[str]) -> dict:
    p = Path(path) if path else ROOT / "config" / "lister.yaml"
    return yaml.safe_load(p.read_text())


def build(cfg: dict, session=None, secrets=None, sleep=None) -> Ctx:
    """Wire everything up. Pass session= (the mock) and secrets= in tests and demos."""
    if session is None:
        import requests

        session = requests.Session()
    get = secrets or keychain.secret
    urls = base_urls(cfg.get("environment", "sandbox"))
    save = None if secrets else (lambda v: keychain.keychain_set(keychain.ITEMS["refresh_token"][1], v))
    oauth = EbayOAuth(session, get("client_id"), get("client_secret"), get("runame") or "", get("refresh_token"),
                      save_refresh_token=save)
    kw = {"sleep": sleep} if sleep else {}
    app_http = EbayHttp(session, token=oauth.app_token, refresh=oauth.drop_app_token, **kw)
    user_http = EbayHttp(session, token=oauth.user_token, refresh=oauth.drop_user_token, **kw)
    state = Path(cfg.get("state_dir", "state"))
    if not state.is_absolute():
        state = ROOT / state
    mk = cfg.get("marketplace", "EBAY_US")
    return Ctx(cfg, oauth, Taxonomy(app_http, urls["api"], state / "cache", marketplace=mk), Inventory(user_http, urls["api"]),
               Account(user_http, urls["api"], mk), Fulfillment(user_http, urls["apiz"]),
               Store(state / "listings.db"), state, app_http, user_http)


def make_lister(ctx: Ctx, provider: Optional[str] = None, drafter=None) -> Lister:
    ai = ctx.cfg.get("ai", {})
    if drafter is None:
        drafter = make_drafter(provider or ai.get("provider", "claude"), float(ai.get("cost_cap_usd", 0.5)), ai.get("model"))
    return Lister(ctx.taxonomy, ctx.inventory, drafter, ctx.store, Setup.load(ctx.setup_path), ctx.cfg.get("category_map"))


def print_draft(d, out=print) -> None:
    L = d.listing
    out(f"[{d.status}] {d.item.sku}")
    if L:
        out(f"  title     {L['title']}  ({len(L['title'])}/80)")
        out(f"  category  {L['category_id']}  {L['category_path']}")
        out(f"  condition {L['condition']}")
        out(f"  price     {L['price']:.2f} {L['currency']}  ({L['ai']['price_from']}; AI range "
            f"{L['ai']['price_low']}-{L['ai']['price_high']}): {L['ai']['price_reasoning'][:140]}"
            if isinstance(L.get("price"), (int, float)) else f"  price     {L.get('price')!r}")
        for k, v in L["aspects"].items():
            out(f"    {k}: {', '.join(v)}")
        for c in L["ai"]["check"]:
            out(f"  check: {c}")
    for p in d.problems:
        out(f"  {p}")
    for n in d.notes:
        out(f"  note: {n}")


def _items(args) -> list[ItemInput]:
    if args.csv:
        return read_csv(Path(args.csv))
    photos = sorted(p for p in Path(args.photos).iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")) if args.photos else []
    row = {"sku": args.sku or "", "note": args.note or "", "photos": ";".join(p.name for p in photos)}
    return [ItemInput(sku=make_sku(row), note=args.note or "", photos=photos, image_urls=args.image_url or [],
                      quantity=args.quantity, category_id=args.category or "", price=args.price, floor=args.floor,
                      ceiling=args.ceiling)]


def cmd_check(ctx: Ctx, args) -> int:
    ok = True
    try:
        ctx.oauth.app_token()
        print(f"application token: ok; category tree for {ctx.taxonomy.marketplace}: {ctx.taxonomy.tree_id()}")
    except (EbayApiError, ValueError) as e:
        print(f"application token: FAILED {e}")
        return 1
    try:
        ctx.oauth.user_token()
        print("seller (user) token: ok")
        print(f"business policies opted in: {'SELLING_POLICY_MANAGEMENT' in ctx.account.opted_in()}")
    except (EbayApiError, ValueError) as e:
        print(f"seller (user) token: not ready ({e})")
        ok = False
    s = Setup.load(ctx.setup_path)
    print(f"seller setup: {'location ' + s.merchant_location_key + ', 3 policies' if s else 'not done (run setup-sandbox)'}")
    return 0 if ok else 1


def cmd_consent(ctx: Ctx, args) -> int:
    print(ctx.oauth.consent_url(state="ebay-lister"))
    return 0


def cmd_exchange(ctx: Ctx, args) -> int:
    raw = args.code.strip()
    code = parse_qs(urlparse(raw).query).get("code", [raw])[0] if raw.startswith("http") else raw
    ctx.oauth.exchange_code(code)
    where = "Keychain ebay-sandbox-refresh-token" if ctx.oauth.save_refresh_token else "this session only"
    print(f"refresh token saved ({where}); seller token ok")
    return 0


def cmd_setup(ctx: Ctx, args) -> int:
    setup, said = setup_sandbox(ctx.account, ctx.inventory, ctx.cfg)
    setup.save(ctx.setup_path)
    for s in said:
        print(s)
    print(f"saved {ctx.setup_path.name}")
    return 0


def cmd_categories(ctx: Ctx, args) -> int:
    for s in ctx.taxonomy.suggest(args.query):
        print(f"{s['categoryId']:>8}  {s['path']}")
    return 0


def cmd_aspects(ctx: Ctx, args) -> int:
    for a in ctx.taxonomy.aspects(args.category_id):
        c = a["aspectConstraint"]
        vals = [v["localizedValue"] for v in a.get("aspectValues", [])][:8]
        print(f"{'REQUIRED ' if c.get('aspectRequired') else '         '}{a['localizedAspectName']:<20} "
              f"{c.get('aspectMode', ''):<15}{c.get('itemToAspectCardinality', ''):<7}{', '.join(vals)}")
    return 0


def cmd_draft(ctx: Ctx, args, publish_step: Optional[bool] = None, drafter=None) -> int:
    lister = make_lister(ctx, args.provider, drafter)
    items = _items(args)
    out_dir = Path(args.out) if args.out else ctx.state / "previews"
    out_dir.mkdir(parents=True, exist_ok=True)
    results, drafts = [], []
    for item in items:
        try:
            d = lister.draft(item, redraft=args.redraft)
        except CostCapReached as e:
            print(f"stopped: {e}")
            break
        drafts.append(d)
        print_draft(d)
        if publish_step is not None:
            r = lister.send(d, publish=publish_step)
            results.append(r)
            print(f"  -> {r['status']}" + (f" offer {r['offerId']}" if r["offerId"] else "")
                  + (f" listing {r['listingId']}" if r["listingId"] else "") + (f"  {r['error']}" if r["error"] else ""))
        print()
    (out_dir / "drafts.json").write_text(json.dumps([d.to_dict() for d in drafts], indent=2))
    costs = lister.drafter.costs
    counts = {}
    for d in drafts:
        counts[d.status] = counts.get(d.status, 0) + 1
    print(f"{len(drafts)} items: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))
    print(f"AI: {costs.calls} calls, {costs.input_tokens} in / {costs.output_tokens} out tokens, ${costs.spent:.4f}")
    if publish_step is not None:
        print(f"eBay: {ctx.user_http.calls + ctx.app_http.calls} API calls, {ctx.user_http.retries + ctx.app_http.retries} retries")
    return 0


def cmd_list(ctx: Ctx, args, drafter=None) -> int:
    return cmd_draft(ctx, args, publish_step=not args.no_publish, drafter=drafter)


def cmd_orders(ctx: Ctx, args) -> int:
    orders = ctx.fulfillment.orders("NOT_STARTED|IN_PROGRESS" if args.open else None)
    for o in orders:
        items = ", ".join(f"{li['quantity']} x {li.get('sku') or '?'}" for li in o.get("lineItems", []))
        t = o["pricingSummary"]["total"]
        print(f"{o['orderId']}  {o['creationDate'][:10]}  {o['orderFulfillmentStatus']:<12} {t['value']:>8} {t['currency']}  "
              f"{o['buyer']['username']}  {items}")
    print(f"{len(orders)} orders" + (" still to ship" if args.open else ""))
    return 0


def read_competitors(path: Optional[str]) -> dict:
    out: dict = {}
    if path:
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                out.setdefault(r["sku"].strip(), []).append(float(r["price"]))
    return out


def cmd_reprice(ctx: Ctx, args, now=None) -> int:
    rules = yaml.safe_load(Path(args.rules).read_text()) if args.rules else {}
    decisions = reprice(ctx.inventory, ctx.fulfillment, ctx.store, rules, read_competitors(args.competitors), args.apply, now)
    for d in decisions:
        if d.skipped:
            print(f"{d.sku}  {d.old:.2f}  skipped: {d.skipped}")
        else:
            print(f"{d.sku}  {d.old:.2f} -> {d.new:.2f}  {'changed' if d.changed else 'no change'}  ({'; '.join(d.reasons)})")
    print(("applied" if args.apply else "dry run, nothing sent") + f": {sum(d.changed for d in decisions)} price changes")
    return 0


def cmd_report(ctx: Ctx, args) -> int:
    rows = ctx.store.all()
    for r in rows:
        print(f"{r['sku']:<18} {r['status']:<14} {r['price'] or 0:>8.2f}  {r['listing_id'] or '-':<14} {(r['title'] or '')[:60]}")
        for p in json.loads(r["problems"] or "[]"):
            print(f"{'':<20}{p}")
    print(f"{len(rows)} SKUs")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ebay-lister", description="AI listings for the eBay sandbox")
    p.add_argument("--config")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    sub.add_parser("consent-url")
    e = sub.add_parser("exchange-code")
    e.add_argument("code")
    sub.add_parser("setup-sandbox")
    c = sub.add_parser("categories")
    c.add_argument("query")
    a = sub.add_parser("aspects")
    a.add_argument("category_id")
    for name in ("draft", "list"):
        d = sub.add_parser(name)
        d.add_argument("--csv")
        d.add_argument("--photos")
        d.add_argument("--note")
        d.add_argument("--sku")
        d.add_argument("--category")
        d.add_argument("--image-url", action="append")
        d.add_argument("--quantity", type=int, default=1)
        d.add_argument("--price", type=float)
        d.add_argument("--floor", type=float)
        d.add_argument("--ceiling", type=float)
        d.add_argument("--provider", choices=["claude", "gemini"])
        d.add_argument("--out")
        d.add_argument("--redraft", action="store_true", help="ask the AI again even if the input has not changed")
        if name == "list":
            d.add_argument("--no-publish", action="store_true")
    o = sub.add_parser("orders")
    o.add_argument("--open", action="store_true")
    r = sub.add_parser("reprice")
    r.add_argument("--rules", default="config/repricing.yaml")
    r.add_argument("--competitors")
    r.add_argument("--apply", action="store_true")
    sub.add_parser("report")
    return p


COMMANDS = {"check": cmd_check, "consent-url": cmd_consent, "exchange-code": cmd_exchange, "setup-sandbox": cmd_setup,
            "categories": cmd_categories, "aspects": cmd_aspects, "draft": lambda c, a: cmd_draft(c, a),
            "list": lambda c, a: cmd_list(c, a), "orders": cmd_orders, "reprice": cmd_reprice, "report": cmd_report}


def main(argv=None, session=None, secrets=None) -> int:
    args = parser().parse_args(argv)
    if args.cmd in ("draft", "list") and not (args.csv or args.photos or args.note):
        print("give --csv, or --photos and/or --note", file=sys.stderr)
        return 2
    ctx = build(load_config(args.config), session, secrets)
    try:
        return COMMANDS[args.cmd](ctx, args)
    except EbayApiError as e:
        print(f"eBay error: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
