from datetime import datetime, timedelta, timezone

import pytest

from ebay_lister.mock_ebay import CATEGORIES
from ebay_lister.repricing import decide, last_sold
from ebay_lister.sku import check_sku, make_sku
from ebay_lister.validate import check_listing, errors

ASPECTS = CATEGORIES["15709"]["aspects"]


def good(**over):
    L = {"title": "Nike Pegasus 40 Mens Running Shoes Size 10", "category_id": "15709", "condition": "USED_EXCELLENT",
         "aspects": {"Brand": ["Nike"], "US Shoe Size": ["10"], "Department": ["Men"], "Type": ["Athletic"],
                     "Style": ["Running Shoe"], "Color": ["Blue", "White"]},
         "description_html": "<p>Worn twice.</p>", "price": 62.0, "currency": "USD", "quantity": 1,
         "image_urls": ["https://i.example.com/a.jpg"]}
    L.update(over)
    return L


def msgs(L, **kw):
    return [str(p) for p in errors(check_listing(L, ASPECTS, **kw))]


def test_a_good_listing_has_no_errors():
    assert msgs(good()) == []


def test_title_limit_is_80():
    assert msgs(good(title="x" * 80)) == []
    assert any("81 characters" in m for m in msgs(good(title="x" * 81)))


def test_missing_required_aspect():
    L = good()
    del L["aspects"]["Department"]
    assert msgs(L) == ["error: aspects.Department: required by this category but missing"]


def test_blank_values_count_as_missing():
    L = good()
    L["aspects"]["Brand"] = [" "]
    assert "aspects.Brand" in msgs(L)[0]


def test_selection_only_value_must_be_from_the_list():
    L = good()
    L["aspects"]["US Shoe Size"] = ["10 UK"]
    assert "not one of this category's values" in msgs(L)[0]


def test_single_cardinality():
    L = good()
    L["aspects"]["Brand"] = ["Nike", "adidas"]
    assert "only one value" in msgs(L)[0]


def test_value_length_limit():
    L = good()
    L["aspects"]["Model"] = ["x" * 51]
    assert "longer than 50" in msgs(L)[0]


def test_unknown_aspect_is_only_a_warning():
    L = good()
    L["aspects"]["Vibe"] = ["fast"]
    p = check_listing(L, ASPECTS)
    assert not errors(p) and any(x.severity == "warning" and "Vibe" in x.field for x in p)


@pytest.mark.parametrize("cond", ["USED", "Brand New", None])
def test_condition_must_be_an_ebay_condition(cond):
    assert any("condition" in m for m in msgs(good(condition=cond)))


@pytest.mark.parametrize("price", [0, -1, "62", None, True])
def test_price_must_be_positive_number(price):
    assert any("price" in m for m in msgs(good(price=price)))


def test_price_decimals():
    assert any("two decimal" in m for m in msgs(good(price=62.555)))


def test_price_floor_and_ceiling():
    assert any("below the floor" in m for m in msgs(good(price=39.0), floor=40))
    assert any("above the ceiling" in m for m in msgs(good(price=91.0), ceiling=90))
    assert msgs(good(price=40.0), floor=40, ceiling=90) == []


@pytest.mark.parametrize("q", [0, 1.5, "2", None])
def test_quantity(q):
    assert any("quantity" in m for m in msgs(good(quantity=q)))


def test_images_must_exist_and_be_https():
    assert any("at least one picture" in m for m in msgs(good(image_urls=[])))
    assert any("not an https link" in m for m in msgs(good(image_urls=["http://x/a.jpg"])))
    assert any("24" in m for m in msgs(good(image_urls=["https://x/a.jpg"] * 25)))


@pytest.mark.parametrize("bad", ["<script>alert(1)</script>", "<p onclick='x()'>hi</p>", "<a href='https://mysite.com'>shop</a>",
                                 "<iframe src='x'></iframe>", "<form action='x'></form>", "<p style='x:expression(1)'>"])
def test_active_content_and_links_in_description(bad):
    assert any("description_html" in m for m in msgs(good(description_html=bad)))


def test_currency_and_category():
    assert any("currency" in m for m in msgs(good(currency="dollars")))
    assert any("category_id" in m for m in msgs(good(category_id="")))


# --- SKUs ---------------------------------------------------------------------------
def test_sku_is_stable_and_ignores_case_and_spaces():
    a = make_sku({"brand": "Nike", "note": "pegasus 40  size 10"})
    b = make_sku({"brand": "nike", "note": "Pegasus 40 size 10"})
    assert a == b and a.startswith("AI-") and len(a) == 15


def test_given_sku_is_kept_and_checked():
    assert make_sku({"sku": "MY-SKU_1.2"}) == "MY-SKU_1.2"
    with pytest.raises(ValueError):
        check_sku("has space")
    with pytest.raises(ValueError):
        check_sku("x" * 51)
    with pytest.raises(ValueError):
        make_sku({})


# --- repricing ------------------------------------------------------------------------
NOW = datetime(2026, 11, 6, tzinfo=timezone.utc)
RULES = {"defaults": {"undercut": 0.50, "max_step_pct": 10, "stale_days": 14, "stale_drop_pct": 5, "round_99": True},
         "skus": {"A": {"floor": 40, "ceiling": 80}}}


def test_no_floor_means_no_change():
    d = decide("B", 60.0, RULES, [30.0], now=NOW)
    assert d.skipped and d.new == 60.0 and not d.changed


def test_undercuts_lowest_competitor_ending_99():
    d = decide("A", 60.0, RULES, [58.0, 65.0], now=NOW)
    assert d.new == 56.99 and d.changed


def test_step_is_capped():
    d = decide("A", 60.0, RULES, [45.0], now=NOW)
    assert d.new == 54.99 and any("10% step" in r for r in d.reasons)


def test_never_below_floor_even_with_cheap_competitors():
    d = decide("A", 42.0, RULES, [10.0], now=NOW)
    assert d.new == 40.0 and any("floor" in r for r in d.reasons)


def test_rounding_goes_up_if_down_would_cross_the_floor():
    d = decide("A", 41.0, {"skus": {"A": {"floor": 40.5}}}, [40.9], now=NOW)
    assert d.new >= 40.5


def test_ceiling_holds():
    d = decide("A", 79.0, RULES, [120.0], now=NOW)
    assert d.new == 80.0


def test_stale_listing_drops():
    d = decide("A", 60.0, RULES, None, sold_at=NOW - timedelta(days=20), now=NOW)
    assert d.new == 56.99 and any("no sale for 20 days" in r for r in d.reasons)


def test_recent_sale_keeps_price():
    d = decide("A", 60.0, RULES, None, sold_at=NOW - timedelta(days=3), now=NOW)
    assert not d.changed


def test_ceiling_below_floor_is_refused():
    d = decide("A", 60.0, {"skus": {"A": {"floor": 50, "ceiling": 40}}}, [55.0], now=NOW)
    assert d.skipped and "below floor" in d.skipped


@pytest.mark.parametrize("current,comp", [(40.0, 1.0), (40.01, 39.0), (500.0, 0.5), (41.0, 40.0)])
def test_floor_property(current, comp):
    for step in (5, 10, 50, 100):
        rules = {"skus": {"A": {"floor": 40, "max_step_pct": step}}}
        assert decide("A", current, rules, [comp], sold_at=NOW - timedelta(days=99), now=NOW).new >= 40


def test_last_sold_from_orders():
    orders = [{"creationDate": "2026-10-01T10:00:00.000Z", "lineItems": [{"sku": "A"}]},
              {"creationDate": "2026-10-20T10:00:00.000Z", "lineItems": [{"sku": "A"}, {"sku": "B"}]}]
    got = last_sold(orders)
    assert got["A"].day == 20 and got["B"].month == 10
