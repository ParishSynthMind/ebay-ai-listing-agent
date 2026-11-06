"""The API clients against the mock, including eBay's real error ids."""
import json

import pytest

from ebay_lister.http import EbayApiError


def item(title="Nike Pegasus 40 Running Shoes", aspects=None, images=True, qty=1, condition="USED_EXCELLENT"):
    return {"availability": {"shipToLocationAvailability": {"quantity": qty}}, "condition": condition,
            "product": {"title": title, "description": "x",
                        "aspects": aspects if aspects is not None else {"Brand": ["Nike"], "US Shoe Size": ["10"], "Department": ["Men"],
                                                                        "Type": ["Athletic"], "Style": ["Running Shoe"], "Color": ["Blue"]},
                        "imageUrls": ["https://i.example.com/a.jpg"] if images else []}}


def offer(ctx, sku="SKU1", category="15709", price="50.00", **over):
    from ebay_lister.lister import Setup

    s = Setup.load(ctx.setup_path)
    o = {"sku": sku, "marketplaceId": "EBAY_US", "format": "FIXED_PRICE", "availableQuantity": 1, "categoryId": category,
         "listingDescription": "<p>x</p>", "merchantLocationKey": s.merchant_location_key,
         "listingPolicies": {"fulfillmentPolicyId": s.fulfillment_policy_id, "paymentPolicyId": s.payment_policy_id,
                             "returnPolicyId": s.return_policy_id},
         "pricingSummary": {"price": {"value": price, "currency": "USD"}}}
    o.update(over)
    return o


def test_default_tree_and_suggestions(ctx):
    assert ctx.taxonomy.tree_id() == "0"
    s = ctx.taxonomy.suggest("mens running shoes")
    assert s[0]["categoryId"] == "15709"
    assert s[0]["path"] == "Clothing, Shoes & Accessories > Men > Men's Shoes > Athletic Shoes"


def test_no_suggestions_is_an_empty_list(ctx):
    assert ctx.taxonomy.suggest("zzzz qqqq") == []


def test_aspects_and_disk_cache(ctx, fake):
    a = ctx.taxonomy.aspects("15709")
    req = [x["localizedAspectName"] for x in a if x["aspectConstraint"]["aspectRequired"]]
    assert req == ["Brand", "US Shoe Size", "Department", "Type", "Style", "Color"]
    before = fake.calls("GET", "/commerce/taxonomy/v1/category_tree/0/get_item_aspects_for_category")
    assert ctx.taxonomy.aspects("15709") == a
    assert fake.calls("GET", "/commerce/taxonomy/v1/category_tree/0/get_item_aspects_for_category") == before


def test_aspects_of_a_non_leaf_category_is_62009(ctx):
    with pytest.raises(EbayApiError) as e:
        ctx.taxonomy.aspects("11450")
    assert e.value.error_ids == [62009]


def test_put_item_is_create_then_replace(ctx, fake):
    ctx.inventory.put_item("SKU1", item())
    ctx.inventory.put_item("SKU1", item(title="Changed"))
    assert ctx.inventory.get_item("SKU1")["product"]["title"] == "Changed"
    assert len(fake.items) == 1


def test_missing_item_is_none(ctx):
    assert ctx.inventory.get_item("NOPE") is None


def test_content_language_is_required(ctx, fake):
    ctx.oauth.user_token()
    r = fake.request("PUT", "https://api.sandbox.ebay.com/sell/inventory/v1/inventory_item/A1",
                     headers={"Authorization": "Bearer " + ctx.oauth.user_token()}, data=json.dumps(item()))
    assert r.status_code == 400 and r.json()["errors"][0]["errorId"] == 25709


def test_bad_sku_is_25707(ctx):
    with pytest.raises(EbayApiError) as e:
        ctx.inventory.put_item("bad sku!", item())
    assert e.value.error_ids == [25707]


def test_title_over_80_is_refused_by_ebay(ctx):
    with pytest.raises(EbayApiError) as e:
        ctx.inventory.put_item("SKU1", item(title="x" * 81))
    assert e.value.error_ids == [25709]


def test_offer_for_unknown_sku_is_25702(ready_ctx):
    with pytest.raises(EbayApiError) as e:
        ready_ctx.inventory.create_offer(offer(ready_ctx, sku="GHOST"))
    assert e.value.error_ids == [25702]


def test_second_offer_for_same_sku_is_refused_and_names_the_first(ready_ctx):
    ready_ctx.inventory.put_item("SKU1", item())
    oid = ready_ctx.inventory.create_offer(offer(ready_ctx))
    with pytest.raises(EbayApiError) as e:
        ready_ctx.inventory.create_offer(offer(ready_ctx))
    assert e.value.error_ids == [25002] and "Offer entity already exists" in e.value.messages()[0]
    assert e.value.errors[0]["parameters"] == [{"name": "offerId", "value": oid}]


def test_offers_for_sku_with_none_is_empty(ready_ctx):
    assert ready_ctx.inventory.offers_for("SKU1") == []


def test_publish_returns_listing_id(ready_ctx):
    ready_ctx.inventory.put_item("SKU1", item())
    oid = ready_ctx.inventory.create_offer(offer(ready_ctx))
    lid = ready_ctx.inventory.publish_offer(oid)
    assert lid.startswith("1100")
    got = ready_ctx.inventory.offers_for("SKU1")[0]
    assert got["status"] == "PUBLISHED" and got["listing"]["listingId"] == lid


@pytest.mark.parametrize("change,eid", [
    ({"listingPolicies": {}}, 25017),
    ({"listingPolicies": {"fulfillmentPolicyId": "999", "paymentPolicyId": "1", "returnPolicyId": "2"}}, 25007),
    ({"merchantLocationKey": "nowhere"}, 25012),
    ({"categoryId": "11450"}, 25005),
    ({"pricingSummary": {"price": {"value": "0.00", "currency": "USD"}}}, 25003),
])
def test_publish_offer_errors(ready_ctx, change, eid):
    ready_ctx.inventory.put_item("SKU1", item())
    oid = ready_ctx.inventory.create_offer(offer(ready_ctx, **change))
    with pytest.raises(EbayApiError) as e:
        ready_ctx.inventory.publish_offer(oid)
    assert e.value.error_ids == [eid]


@pytest.mark.parametrize("it,eid", [(item(images=False), 25014), (item(qty=0), 25004), (item(condition=None), 25021)])
def test_publish_item_errors(ready_ctx, it, eid):
    ready_ctx.inventory.put_item("SKU1", it)
    oid = ready_ctx.inventory.create_offer(offer(ready_ctx))
    with pytest.raises(EbayApiError) as e:
        ready_ctx.inventory.publish_offer(oid)
    assert e.value.error_ids == [eid]


def test_publish_with_missing_required_item_specific(ready_ctx):
    ready_ctx.inventory.put_item("SKU1", item(aspects={"US Shoe Size": ["10"]}))
    oid = ready_ctx.inventory.create_offer(offer(ready_ctx))
    with pytest.raises(EbayApiError) as e:
        ready_ctx.inventory.publish_offer(oid)
    assert e.value.error_ids == [25002]
    assert "The item specific Brand is missing" in e.value.messages()[0]


def test_policies_need_opt_in_first(ctx):
    with pytest.raises(EbayApiError) as e:
        ctx.account.policies("fulfillment")
    assert e.value.error_ids == [20403]
    ctx.account.opt_in_policies()
    assert ctx.account.opted_in() == ["SELLING_POLICY_MANAGEMENT"]
    with pytest.raises(EbayApiError) as e:
        ctx.account.opt_in_policies()
    assert e.value.status == 409


def test_return_policy_needs_returns_accepted(ctx):
    ctx.account.opt_in_policies()
    with pytest.raises(EbayApiError):
        ctx.account.create_policy("return", {"name": "r", "marketplaceId": "EBAY_US", "categoryTypes": [{"name": "ALL_EXCLUDING_MOTORS_VEHICLES"}]})


def test_location_twice_is_409(ctx):
    body = ctx.cfg["location"]["body"]
    ctx.inventory.create_location("L1", body)
    assert ctx.inventory.get_location("L1")["merchantLocationKey"] == "L1"
    with pytest.raises(EbayApiError) as e:
        ctx.inventory.create_location("L1", body)
    assert e.value.status == 409 and e.value.error_ids == [25803]


def test_orders_follow_paging_and_filter(ctx, fake):
    for i in range(7):
        fake.add_order(f"S{i}", f"item {i}", 10.0 + i, status="FULFILLED" if i % 2 else "NOT_STARTED")
    assert len(ctx.fulfillment.orders(limit=3)) == 7
    open_ = ctx.fulfillment.orders("NOT_STARTED|IN_PROGRESS", limit=2)
    assert [o["lineItems"][0]["sku"] for o in open_] == ["S0", "S2", "S4", "S6"]
    assert fake.calls("GET", "/sell/fulfillment/v1/order") == 3 + 2


def test_orders_limit_over_200_is_refused(ctx):
    with pytest.raises(EbayApiError):
        ctx.fulfillment.orders(limit=201)


def test_fulfillment_is_on_the_apiz_host(ctx, fake):
    ctx.fulfillment.orders()
    assert ctx.fulfillment.base.startswith("https://apiz.sandbox.ebay.com/")
