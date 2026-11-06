"""eBay OAuth: an application token (client credentials) and a user token (authorization code, then refresh).

- Application token: for the Taxonomy API, which reads public data. Scope api_scope.
- User token: for the Sell APIs (Inventory, Account, Fulfillment), which act for one seller.
  The seller consents once in a browser; the code that comes back is swapped for an access
  token (2 hours) and a refresh token (about 18 months). After that the access token is
  renewed from the refresh token with no browser.

Sandbox only: this build refuses the production endpoints.
"""
from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlencode

from .http import EbayApiError, parse_errors

SANDBOX = {
    "api": "https://api.sandbox.ebay.com",
    "apiz": "https://apiz.sandbox.ebay.com",  # the Fulfillment API lives on apiz (eBay's OpenAPI spec)
    "auth": "https://auth.sandbox.ebay.com",
}
APP_SCOPE = "https://api.ebay.com/oauth/api_scope"
USER_SCOPES = [
    "https://api.ebay.com/oauth/api_scope",
    "https://api.ebay.com/oauth/api_scope/sell.inventory",
    "https://api.ebay.com/oauth/api_scope/sell.account",
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment",
]
EARLY = 300  # renew five minutes before eBay says the token expires


def base_urls(environment: str = "sandbox") -> dict:
    if environment != "sandbox":
        raise ValueError("this build only talks to the eBay sandbox; production is switched off on purpose")
    return SANDBOX


@dataclass
class Token:
    value: str
    expires_at: float

    def fresh(self, now: float) -> bool:
        return now < self.expires_at - EARLY


class EbayOAuth:
    def __init__(self, session, client_id: str, client_secret: str, runame: str = "",
                 refresh_token: Optional[str] = None, environment: str = "sandbox",
                 clock: Callable[[], float] = time.time, save_refresh_token: Optional[Callable[[str], None]] = None):
        if not client_id or not client_secret:
            raise ValueError("eBay client id and client secret are needed (Keychain ebay-sandbox-client-id / -secret)")
        self.session = session
        self.client_id = client_id
        self.client_secret = client_secret
        self.runame = runame
        self.refresh_token = refresh_token
        self.urls = base_urls(environment)
        self.clock = clock
        self.save_refresh_token = save_refresh_token
        self._app: Optional[Token] = None
        self._user: Optional[Token] = None
        self.token_requests = 0

    # --- the token endpoint ------------------------------------------------
    def _basic(self) -> str:
        return "Basic " + base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()

    def _token_call(self, form: dict) -> dict:
        self.token_requests += 1
        resp = self.session.request(
            "POST", self.urls["api"] + "/identity/v1/oauth2/token", data=form, timeout=30,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Authorization": self._basic()},
        )
        if resp.status_code >= 400:
            raise EbayApiError(resp.status_code, parse_errors(resp), "/identity/v1/oauth2/token", "POST")
        return resp.json()

    # --- application token ---------------------------------------------------
    def app_token(self) -> str:
        if self._app is None or not self._app.fresh(self.clock()):
            body = self._token_call({"grant_type": "client_credentials", "scope": APP_SCOPE})
            self._app = Token(body["access_token"], self.clock() + int(body["expires_in"]))
        return self._app.value

    def drop_app_token(self) -> None:
        self._app = None

    # --- user token ------------------------------------------------------------
    def consent_url(self, state: str = "", scopes: list[str] = USER_SCOPES) -> str:
        if not self.runame:
            raise ValueError("a RuName is needed for the consent page (Keychain ebay-sandbox-runame)")
        q = {"client_id": self.client_id, "redirect_uri": self.runame, "response_type": "code", "scope": " ".join(scopes)}
        if state:
            q["state"] = state
        return self.urls["auth"] + "/oauth2/authorize?" + urlencode(q)

    def exchange_code(self, code: str) -> str:
        """Swap the one-time code from the consent page for tokens. Returns the refresh token (also saved)."""
        body = self._token_call({"grant_type": "authorization_code", "code": code, "redirect_uri": self.runame})
        self._user = Token(body["access_token"], self.clock() + int(body["expires_in"]))
        self.refresh_token = body["refresh_token"]
        if self.save_refresh_token:
            self.save_refresh_token(self.refresh_token)
        return self.refresh_token

    def user_token(self) -> str:
        if self._user is None or not self._user.fresh(self.clock()):
            if not self.refresh_token:
                raise ValueError("no seller refresh token yet: run `ebay-lister consent-url`, then `ebay-lister exchange-code`")
            body = self._token_call({"grant_type": "refresh_token", "refresh_token": self.refresh_token,
                                     "scope": " ".join(USER_SCOPES)})
            self._user = Token(body["access_token"], self.clock() + int(body["expires_in"]))
        return self._user.value

    def drop_user_token(self) -> None:
        self._user = None
