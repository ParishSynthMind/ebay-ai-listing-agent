import random
from urllib.parse import parse_qs, urlparse

import pytest

from ebay_lister.auth import APP_SCOPE, USER_SCOPES, EbayOAuth, base_urls
from ebay_lister.http import EbayApiError, EbayHttp, parse_errors
from ebay_lister.mock_ebay import FakeEbay, FakeResponse, err


def oauth(fake, refresh=None, **kw):
    return EbayOAuth(fake, fake.client_id, fake.client_secret, fake.runame, refresh, clock=lambda: fake.now, **kw)


def test_production_is_refused():
    with pytest.raises(ValueError, match="sandbox"):
        base_urls("production")


def test_missing_keys_explain_where_they_go():
    with pytest.raises(ValueError, match="Keychain"):
        EbayOAuth(FakeEbay(), "", "")


def test_app_token_is_cached_until_near_expiry():
    f = FakeEbay()
    o = oauth(f)
    t1 = o.app_token()
    assert o.app_token() == t1 and o.token_requests == 1
    f.now += 7200 - 200  # inside the five-minute early window
    assert o.app_token() != t1 and o.token_requests == 2


def test_app_token_asks_for_the_public_scope_only():
    f = FakeEbay()
    o = oauth(f)
    o.app_token()
    assert f.tokens[o._app.value]["scopes"] == [APP_SCOPE]


def test_wrong_secret_is_invalid_client():
    f = FakeEbay()
    o = EbayOAuth(f, f.client_id, "wrong", clock=lambda: f.now)
    with pytest.raises(EbayApiError) as e:
        o.app_token()
    assert e.value.status == 401 and "invalid_client" in str(e.value)


def test_consent_url_has_runame_scopes_and_state():
    f = FakeEbay()
    u = urlparse(oauth(f).consent_url(state="xyz"))
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    assert u.netloc == "auth.sandbox.ebay.com" and u.path == "/oauth2/authorize"
    assert q["redirect_uri"] == f.runame and q["response_type"] == "code" and q["state"] == "xyz"
    assert q["scope"].split() == USER_SCOPES


def test_consent_url_needs_a_runame():
    f = FakeEbay()
    with pytest.raises(ValueError, match="RuName"):
        EbayOAuth(f, f.client_id, f.client_secret).consent_url()


def test_code_exchange_saves_refresh_token_and_code_is_one_time():
    f = FakeEbay()
    saved = []
    o = oauth(f, save_refresh_token=saved.append)
    code = f.issue_code()
    rt = o.exchange_code(code)
    assert saved == [rt] and rt in f.refresh_tokens
    assert o.user_token()  # usable straight away, no second call
    assert o.token_requests == 1
    with pytest.raises(EbayApiError, match="invalid_grant"):
        oauth(f).exchange_code(code)


def test_code_exchange_needs_the_same_runame():
    f = FakeEbay()
    o = EbayOAuth(f, f.client_id, f.client_secret, "Other-RuName", clock=lambda: f.now)
    with pytest.raises(EbayApiError, match="invalid_grant"):
        o.exchange_code(f.issue_code())


def test_user_token_from_refresh_token_and_renewal():
    f = FakeEbay()
    f.refresh_tokens.add("rt1")
    o = oauth(f, refresh="rt1")
    t1 = o.user_token()
    assert o.user_token() == t1
    f.now += 7200
    assert o.user_token() != t1 and o.token_requests == 2


def test_user_token_without_refresh_token_says_what_to_do():
    with pytest.raises(ValueError, match="consent-url"):
        oauth(FakeEbay()).user_token()


def test_bad_refresh_token():
    with pytest.raises(EbayApiError, match="invalid_grant"):
        oauth(FakeEbay(), refresh="nope").user_token()


def test_app_token_cannot_call_sell_apis():
    f = FakeEbay()
    o = oauth(f)
    h = EbayHttp(f, token=o.app_token)
    with pytest.raises(EbayApiError) as e:
        h.json("GET", "https://api.sandbox.ebay.com/sell/inventory/v1/inventory_item/X1")
    assert e.value.status == 403 and e.value.error_ids == [1100]


def test_expired_token_is_refreshed_once_and_call_repeated():
    f = FakeEbay()
    f.refresh_tokens.add("rt1")
    o = oauth(f, refresh="rt1")
    h = EbayHttp(f, token=o.user_token, refresh=o.drop_user_token, sleep=lambda s: None)
    o.user_token()
    f.expire_tokens()  # eBay revoked it early; our clock still thinks it is fresh
    body = h.json("GET", "https://api.sandbox.ebay.com/sell/account/v1/program/get_opted_in_programs")
    assert body == {"programs": []} and o.token_requests == 2


def test_401_twice_is_raised_not_looped():
    f = FakeEbay()
    f.inject = [err(401, 1001, "Invalid access token", "OAuth")] * 3
    h = EbayHttp(f, token=lambda: "t", refresh=lambda: None, sleep=lambda s: None)
    with pytest.raises(EbayApiError) as e:
        h.request("GET", "https://api.sandbox.ebay.com/sell/inventory/v1/offer?sku=A")
    assert e.value.status == 401 and len(f.log) == 2


def test_429_and_500_are_retried_with_backoff():
    f = FakeEbay()
    f.refresh_tokens.add("rt1")
    o = oauth(f, refresh="rt1")
    slept = []
    h = EbayHttp(f, token=o.user_token, sleep=slept.append, rng=random.Random(1))
    o.user_token()
    f.inject = [err(429, 2002, "The request limit has been reached for the resource.", "ACCESS"),
                err(500, 25001, "A system error has occurred. ", category="APPLICATION")]
    assert h.json("GET", "https://api.sandbox.ebay.com/sell/account/v1/program/get_opted_in_programs") == {"programs": []}
    assert h.retries == 2 and 1.0 <= slept[0] < 2.0 and 2.0 <= slept[1] < 3.0


def test_retry_after_header_is_honoured():
    f = FakeEbay()
    slept = []
    h = EbayHttp(f, token=lambda: "t", sleep=slept.append)
    f.inject = [FakeResponse(429, {"errors": [{"errorId": 2002}]}, {"Retry-After": "7"}), err(404, 25710, "nope")]
    with pytest.raises(EbayApiError):
        h.request("GET", "https://api.sandbox.ebay.com/sell/inventory/v1/inventory_item/A")
    assert slept == [7.0]


def test_retries_run_out():
    f = FakeEbay()
    f.inject = [err(429, 2002, "limit", "ACCESS")] * 10
    h = EbayHttp(f, token=lambda: "t", sleep=lambda s: None, max_retries=3)
    with pytest.raises(EbayApiError) as e:
        h.request("GET", "https://api.sandbox.ebay.com/x")
    assert e.value.status == 429 and h.retries == 3


def test_user_errors_are_not_retried():
    f = FakeEbay()
    f.inject = [err(400, 25002, "A user error has occurred. ")]
    h = EbayHttp(f, token=lambda: "t", sleep=lambda s: pytest.fail("should not sleep"))
    with pytest.raises(EbayApiError) as e:
        h.request("POST", "https://api.sandbox.ebay.com/x")
    assert e.value.error_ids == [25002]


def test_parse_errors_handles_oauth_and_non_json():
    class R:
        status_code = 400
        text = "<html>bad gateway</html>"

        def json(self):
            raise ValueError

    assert parse_errors(R())[0]["message"].startswith("<html>")
    oauth_style = FakeResponse(400, {"error": "invalid_grant", "error_description": "expired"})
    assert parse_errors(oauth_style) == [{"errorId": 0, "message": "invalid_grant", "longMessage": "expired"}]
