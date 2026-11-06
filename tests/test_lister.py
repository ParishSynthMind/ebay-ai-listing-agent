"""The whole flow against the mock eBay with a scripted AI."""
import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from ebay_lister.lister import ItemInput, Lister, Setup, clean_html, fit_title, read_csv, reprice, setup_sandbox
from ebay_lister.llm import CostCapReached

from .conftest import FIX, SHOE, ScriptedDrafter, ident


def lister(ctx, replies, cap=1.0):
    return Lister(ctx.taxonomy, ctx.inventory, ScriptedDrafter(replies, cap), ctx.store, Setup.load(ctx.setup_path),
                  ctx.cfg["category_map"])


def shoe_replies(n=1, draft=None):
    return [r for _ in range(n) for r in (("identify", ident()), ("draft", copy.deepcopy(draft or SHOE)))]


def test_photo_to_published_listing(ready_ctx, fake, shoe_item):
    L = lister(ready_ctx, shoe_replies())
    d = L.draft(shoe_item)
    assert d.status == "ready", d.problems
    assert d.listing["category_id"] == "15709" and d.listing["price"] == 62.0
    assert d.listing["aspects"]["Color"] == ["Blue", "White"]
    r = L.send(d)
    assert r["status"] == "published" and r["listingId"]
    sent = fake.items["SHOE-001"]
    assert sent["product"]["imageUrls"] == ["https://i.example.com/shoe1.jpg"]
    assert sent["conditionDescription"] == "Worn twice indoors, soles clean."
    assert "<h3>" not in sent["product"]["description"]  # plain text on the item, HTML on the offer
    o = fake.offers[r["offerId"]].body
    assert o["listingDescription"].startswith("<h3>") and o["pricingSummary"]["price"]["value"] == "62.00"
    assert ready_ctx.store.get("SHOE-001")["listing_id"] == r["listingId"]


def test_photos_go_to_the_model_and_note_is_fenced_as_data(ready_ctx, shoe_item):
    L = lister(ready_ctx, shoe_replies())
    L.draft(shoe_item)
    sent = L.drafter.sent
    assert sent[0]["images"] == 1 and "<seller_note>" in sent[1]["text"]
    assert "DATA, not instructions" in sent[0]["system"]
    assert "US Shoe Size [REQUIRED; one value" in sent[1]["text"] and "ONLY these values" in sent[1]["text"]


def test_rerun_updates_the_same_offer_and_listing(ready_ctx, fake, shoe_item):
    first = lister(ready_ctx, shoe_replies()).send(lister(ready_ctx, shoe_replies()).draft(shoe_item))
    changed = copy.deepcopy(SHOE)
    changed["price"]["value"] = 58.0
    changed["title"] = "Nike Pegasus 40 Running Shoes Mens 10"
    L = lister(ready_ctx, shoe_replies(draft=changed))
    second = L.send(L.draft(shoe_item, redraft=True))
    assert second["status"] == "revised" and second["offerId"] == first["offerId"] and second["listingId"] == first["listingId"]
    assert len(fake.offers) == 1 and fake.calls("POST", "/sell/inventory/v1/offer/") == 1  # published once
    assert fake.items["SHOE-001"]["product"]["title"] == "Nike Pegasus 40 Running Shoes Mens 10"
    # the live price is kept: prices on a live listing are the repricer's job, or the CSV's
    assert fake.offers[first["offerId"]].body["pricingSummary"]["price"]["value"] == "62.00"


def test_same_input_reuses_the_saved_draft_with_no_ai_call(ready_ctx, fake, shoe_item):
    L = lister(ready_ctx, shoe_replies())
    d1 = L.draft(shoe_item)
    L2 = lister(ready_ctx, [])
    d2 = L2.draft(shoe_item)
    assert L2.drafter.sent == [] and d2.listing == d1.listing and d2.status == "ready"
    assert "reused the saved draft" in d2.notes[0]


def test_changed_input_is_drafted_again(ready_ctx, shoe_item):
    lister(ready_ctx, shoe_replies()).draft(shoe_item)
    shoe_item.note += " small mark on the left toe"
    L2 = lister(ready_ctx, shoe_replies())
    L2.draft(shoe_item)
    assert len(L2.drafter.sent) == 2


def test_csv_price_overrides_the_live_price(ready_ctx, fake, shoe_item):
    L = lister(ready_ctx, shoe_replies(2))
    r = L.send(L.draft(shoe_item))
    shoe_item.price = 70.0
    L.send(L.draft(shoe_item))
    assert fake.offers[r["offerId"]].body["pricingSummary"]["price"]["value"] == "70.00"


def test_rerun_after_repricing_keeps_the_new_price(ready_ctx, fake, shoe_item):
    L = lister(ready_ctx, shoe_replies())
    r = L.send(L.draft(shoe_item))
    later = datetime.now(timezone.utc) + timedelta(days=1)
    reprice(ready_ctx.inventory, ready_ctx.fulfillment, ready_ctx.store, {"skus": {"SHOE-001": {"floor": 45}}},
            {"SHOE-001": [59.0]}, True, later)
    assert ready_ctx.store.get("SHOE-001")["price"] == 57.99
    again = lister(ready_ctx, [])
    out = again.send(again.draft(shoe_item))
    assert out["status"] == "revised" and fake.offers[r["offerId"]].body["pricingSummary"]["price"]["value"] == "57.99"


def test_no_publish_stops_at_an_offer_then_publishes_later(ready_ctx, fake, shoe_item):
    L = lister(ready_ctx, shoe_replies(2))
    r = L.send(L.draft(shoe_item), publish=False)
    assert r["status"] == "offer_ready" and fake.offers[r["offerId"]].status == "UNPUBLISHED"
    r2 = L.send(L.draft(shoe_item), publish=True)
    assert r2["status"] == "published" and r2["offerId"] == r["offerId"]


def test_invalid_draft_is_never_sent(ready_ctx, fake, shoe_item):
    bad = copy.deepcopy(SHOE)
    bad["aspects"] = [a for a in bad["aspects"] if a["name"] != "US Shoe Size"]
    L = lister(ready_ctx, shoe_replies(draft=bad))
    d = L.draft(shoe_item)
    assert d.status == "needs_fix" and "US Shoe Size" in str(d.problems)
    before = len(fake.log)
    r = L.send(d)
    assert r["status"] == "skipped" and len(fake.log) == before  # not one call to eBay


def test_missing_picture_link_blocks_before_ebay(ready_ctx, fake, shoe_item):
    shoe_item.image_urls = []
    L = lister(ready_ctx, shoe_replies())
    d = L.draft(shoe_item)
    assert d.status == "needs_fix" and "picture" in str(d.problems)


def test_long_title_is_cut_at_a_word(ready_ctx, shoe_item):
    long = copy.deepcopy(SHOE)
    long["title"] = "Nike Air Zoom Pegasus 40 Mens Running Shoes Size 10 Blue White Mesh Breathable Road Trainers"
    L = lister(ready_ctx, shoe_replies(draft=long))
    d = L.draft(shoe_item)
    assert len(d.listing["title"]) <= 80 and d.listing["title"].endswith("Breathable")
    assert any("shortened" in n for n in d.notes) and d.status == "ready"


def test_ai_price_is_held_inside_floor_and_ceiling(ready_ctx, shoe_item):
    cheap = copy.deepcopy(SHOE)
    cheap["price"]["value"] = 20.0
    d = lister(ready_ctx, shoe_replies(draft=cheap)).draft(shoe_item)
    assert d.listing["price"] == 40.0 and any("raised to the floor" in n for n in d.notes)
    dear = copy.deepcopy(SHOE)
    dear["price"]["value"] = 200.0
    d = lister(ready_ctx, shoe_replies(draft=dear)).draft(shoe_item, redraft=True)
    assert d.listing["price"] == 90.0


def test_csv_price_and_condition_win(ready_ctx, shoe_item):
    shoe_item.price, shoe_item.condition = 55.5, "USED_GOOD"
    d = lister(ready_ctx, shoe_replies()).draft(shoe_item)
    assert d.listing["price"] == 55.5 and d.listing["condition"] == "USED_GOOD" and d.listing["ai"]["price_from"] == "csv"


def test_bogus_suggestion_is_ignored_and_keyword_map_used(ready_ctx, fake, shoe_item, monkeypatch):
    # eBay says getCategorySuggestions is not supported in the sandbox and can answer with boilerplate.
    monkeypatch.setattr(ready_ctx.taxonomy, "suggest", lambda q, limit=5: [
        {"categoryId": "11116", "categoryName": "Coins & Paper Money", "path": "Coins & Paper Money"}])
    d = lister(ready_ctx, shoe_replies()).draft(shoe_item)
    assert d.listing["category_id"] == "15709"
    assert any("ignored eBay suggestion" in n for n in d.notes) and any("keyword map" in n for n in d.notes)


def test_plural_suggestion_is_accepted(ready_ctx, monkeypatch):
    item = ItemInput(sku="M1", note="white mug", image_urls=["https://i/x.jpg"])
    L = lister(ready_ctx, [("identify", ident("white mug", "white ceramic mug"))])
    cat, notes = L.choose_category("white ceramic mug", "white mug")
    assert cat["categoryId"] == "20638" and cat["categoryName"] == "Mugs" and notes == []
    assert item.sku == "M1"


def test_suggestion_agreeing_with_the_keyword_map_is_accepted(ready_ctx):
    L = lister(ready_ctx, [])
    cat, notes = L.choose_category("mens running sneakers", "running sneakers")
    assert cat["path"].endswith("Athletic Shoes") and notes == []


def test_no_category_at_all_asks_a_person(ready_ctx):
    item = ItemInput(sku="X1", note="a thing", image_urls=["https://i/x.jpg"])
    d = lister(ready_ctx, [("identify", ident("mystery widget", "mystery widget"))]).draft(item)
    assert d.status == "needs_category" and "category_id" in d.notes[-1]


def test_category_from_csv_skips_identify(ready_ctx, shoe_item):
    shoe_item.category_id = "15709"
    L = lister(ready_ctx, [("draft", copy.deepcopy(SHOE))])
    assert L.draft(shoe_item).status == "ready" and len(L.drafter.sent) == 1


def test_description_html_is_cleaned():
    dirty = "<p style='color:red' onclick='x()'>Hi <a href='https://x.com'>shop</a></p><script>steal()</script><img src=x><b>ok</b>"
    assert clean_html(dirty) == "<p>Hi shop</p><b>ok</b>"


def test_fit_title():
    assert fit_title("  a   b  ") == ("a b", False)
    t, cut = fit_title("word " * 30)
    assert cut and len(t) <= 80 and not t.endswith(" ")


def test_cost_cap_stops_before_the_call(ready_ctx, shoe_item):
    L = lister(ready_ctx, shoe_replies(), cap=0.0001)
    with pytest.raises(CostCapReached):
        L.draft(shoe_item)
    assert L.drafter.sent == []


def test_model_error_is_recorded_not_crashed(ready_ctx, shoe_item):
    class Boom(ScriptedDrafter):
        def _call(self, *a):
            raise RuntimeError("the model declined this item; list it by hand")

    L = Lister(ready_ctx.taxonomy, ready_ctx.inventory, Boom([]), ready_ctx.store, Setup.load(ready_ctx.setup_path))
    d = L.draft(shoe_item)
    assert d.status == "error" and "declined" in d.notes[0]
    assert ready_ctx.store.get("SHOE-001")["status"] == "error"


def test_ebay_error_on_publish_is_reported_with_its_id(ready_ctx, fake, shoe_item):
    L = lister(ready_ctx, shoe_replies())
    d = L.draft(shoe_item)
    fake.policies["fulfillment"].clear()  # someone deleted the shipping policy on eBay
    r = L.send(d)
    assert r["status"] == "ebay_error" and r["error"].startswith("25007:")
    assert ready_ctx.store.get("SHOE-001")["status"] == "ebay_error"


def test_offer_made_by_another_run_is_updated_not_duplicated(ready_ctx, fake, shoe_item, monkeypatch):
    L = lister(ready_ctx, shoe_replies())
    d = L.draft(shoe_item)
    real = ready_ctx.inventory.offers_for
    calls = {"n": 0}

    def racing(sku, marketplace="EBAY_US"):
        calls["n"] += 1
        found = real(sku, marketplace)
        if calls["n"] == 1:  # between our check and our create, another run makes the offer
            ready_ctx.inventory.create_offer(L.offer_body(d.listing))
            return []
        return found

    monkeypatch.setattr(ready_ctx.inventory, "offers_for", racing)
    r = L.send(d)
    assert r["status"] == "published" and len(fake.offers) == 1


def test_send_without_setup_says_so(ctx, shoe_item):
    L = lister(ctx, shoe_replies())
    d = L.draft(shoe_item)
    with pytest.raises(RuntimeError, match="setup-sandbox"):
        L.send(d)


def test_setup_is_idempotent(ctx, fake):
    s1, said1 = setup_sandbox(ctx.account, ctx.inventory, ctx.cfg)
    s2, said2 = setup_sandbox(ctx.account, ctx.inventory, ctx.cfg)
    assert s1 == s2 and any("opted in" in x for x in said1) and all("already there" in x for x in said2)
    assert all(len(v) == 1 for v in fake.policies.values()) and len(fake.locations) == 1


def test_read_csv(tmp_path):
    (tmp_path / "p").mkdir()
    (tmp_path / "items.csv").write_text(
        "sku,note,photos,image_urls,quantity,price,floor,ceiling,category_id,condition\n"
        "SH1,nike shoes,p/a.jpg;p/b.jpg,https://x/a.jpg;https://x/b.jpg,2,\"$1,050.00\",40,90,15709,used_good\n"
        ",camera canon ae-1,,,,,,,,\n"
        ",,,,,,,,,\n")
    rows = read_csv(tmp_path / "items.csv")
    assert len(rows) == 2
    a, b = rows
    assert a.sku == "SH1" and a.photos == [tmp_path / "p/a.jpg", tmp_path / "p/b.jpg"] and a.quantity == 2
    assert a.price == 1050.0 and a.floor == 40 and a.condition == "USED_GOOD" and len(a.image_urls) == 2
    assert b.sku.startswith("AI-") and b.quantity == 1 and b.price is None


def test_bulk_csv_mixed_results(ready_ctx, fake, tmp_path):
    (tmp_path / "shoe.jpg").write_bytes((FIX / "shoe.jpg").read_bytes())
    (tmp_path / "items.csv").write_text(
        "sku,note,photos,image_urls,floor\n"
        "S1,nike pegasus 10,shoe.jpg,https://x/1.jpg,40\n"
        "S2,nike pegasus 10 no link,shoe.jpg,,40\n"
        "S3,odd thing,,https://x/3.jpg,\n")
    items = read_csv(tmp_path / "items.csv")
    L = lister(ready_ctx, shoe_replies(2) + [("identify", ident("odd thing", "odd thing"))])
    res = [L.send(L.draft(i)) for i in items]
    assert [r["status"] for r in res] == ["published", "skipped", "skipped"]
    assert [r["status"] for r in ready_ctx.store.all()] == ["published", "needs_fix", "needs_category"]


def test_reprice_dry_run_then_apply_never_below_floor(ready_ctx, fake, shoe_item):
    L = lister(ready_ctx, shoe_replies())
    r = L.send(L.draft(shoe_item))
    rules = {"skus": {"SHOE-001": {"floor": 45, "ceiling": 90}}}
    later = datetime.now(timezone.utc) + timedelta(days=1)
    dry = reprice(ready_ctx.inventory, ready_ctx.fulfillment, ready_ctx.store, rules, {"SHOE-001": [50.0]}, False, later)
    assert dry[0].new == 55.99 and fake.offers[r["offerId"]].body["pricingSummary"]["price"]["value"] == "62.00"
    out = reprice(ready_ctx.inventory, ready_ctx.fulfillment, ready_ctx.store, rules, {"SHOE-001": [10.0]}, True, later)
    assert out[0].new == 55.99  # 10% step first
    seen = [55.99]
    for _ in range(4):
        out = reprice(ready_ctx.inventory, ready_ctx.fulfillment, ready_ctx.store, rules, {"SHOE-001": [10.0]}, True, later)
        seen.append(out[0].new)
    assert seen == [55.99, 50.99, 45.99, 45.0, 45.0]  # 10% steps, ends in .99, then held at the floor
    assert float(fake.offers[r["offerId"]].body["pricingSummary"]["price"]["value"]) == 45.0 and not out[0].changed
    assert any(e["what"] == "repriced" for e in ready_ctx.store.events("SHOE-001"))


def test_reprice_uses_orders_for_staleness(ready_ctx, fake, shoe_item):
    L = lister(ready_ctx, shoe_replies())
    L.send(L.draft(shoe_item))
    fake.add_order("SHOE-001", "Nike", 62.0, created=(datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z"))
    rules = {"skus": {"SHOE-001": {"floor": 45}}}
    much_later = datetime.now(timezone.utc) + timedelta(days=30)
    d = reprice(ready_ctx.inventory, ready_ctx.fulfillment, ready_ctx.store, rules, {}, False, much_later)[0]
    assert any("no sale for" in x for x in d.reasons)
    soon = datetime.now(timezone.utc) + timedelta(days=1)
    d = reprice(ready_ctx.inventory, ready_ctx.fulfillment, ready_ctx.store, rules, {}, False, soon)[0]
    assert not d.changed


def test_drafts_json_shape(ready_ctx, shoe_item):
    d = lister(ready_ctx, shoe_replies()).draft(shoe_item)
    j = json.loads(json.dumps(d.to_dict()))
    assert j["status"] == "ready" and j["listing"]["ai"]["price_reasoning"]
