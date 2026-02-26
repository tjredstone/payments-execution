from __future__ import annotations

from datetime import date
from urllib.parse import urlencode

import requests

from config import TrueLayerConfig


class TrueLayerClient:
    """
    TrueLayer client for Stage 1 (read-only advisory).

    Sandbox flow (unregulated):
    - Redirect user to TrueLayer Auth Dialog to collect consent
    - Receive `code` in callback
    - Exchange code for access + refresh tokens
    - Use access token to call Data API (accounts, balance, transactions)
    """

    def __init__(self, cfg: TrueLayerConfig):
        self.cfg = cfg

    # ----------------------------
    # Auth Dialog URL (Hosted UI)
    # ----------------------------

    def generate_auth_link(
        self,
        scopes: list[str],
        state: str | None = None,
        providers: list[str] | None = None,
    ) -> str:
        """
        Build the TrueLayer Auth Dialog URL.

        Example:
        https://auth.truelayer-sandbox.com/?response_type=code&client_id=...&redirect_uri=...&scope=...
        """
        params = {
            "response_type": "code",
            "client_id": self.cfg.client_id,
            "redirect_uri": self.cfg.redirect_uri,
            "scope": " ".join(scopes),
        }
        if state:
            params["state"] = state
        if providers:
            params["providers"] = " ".join(providers)

        return f"{self.cfg.auth_base}/?{urlencode(params)}"

    # ----------------------------
    # Token exchange / refresh
    # ----------------------------

    def exchange_code_for_tokens(self, code: str) -> dict:
        """
        Exchange the callback code for access + refresh tokens.
        """
        url = f"{self.cfg.auth_base}/connect/token"

        payload = {
            "grant_type": "authorization_code",
            "client_id": self.cfg.client_id,
            "client_secret": self.cfg.client_secret,
            "redirect_uri": self.cfg.redirect_uri,
            "code": code,
        }

        r = requests.post(url, data=payload, timeout=20)
        r.raise_for_status()
        return r.json()

    def refresh_tokens(self, refresh_token: str) -> dict:
        """
        Refresh an expired access token.
        """
        url = f"{self.cfg.auth_base}/connect/token"

        payload = {
            "grant_type": "refresh_token",
            "client_id": self.cfg.client_id,
            "client_secret": self.cfg.client_secret,
            "refresh_token": refresh_token,
        }

        r = requests.post(url, data=payload, timeout=20)
        r.raise_for_status()
        return r.json()

    # ----------------------------
    # Data API calls (AIS)
    # ----------------------------

    def _headers(self, access_token: str) -> dict:
        return {"Authorization": f"Bearer {access_token}"}

    def list_accounts(self, access_token: str) -> list[dict]:
        url = f"{self.cfg.api_base}/data/v1/accounts"
        r = requests.get(url, headers=self._headers(access_token), timeout=20)
        r.raise_for_status()
        return r.json().get("results", [])

    def get_balance(self, access_token: str, account_id: str) -> list[dict]:
        url = f"{self.cfg.api_base}/data/v1/accounts/{account_id}/balance"
        r = requests.get(url, headers=self._headers(access_token), timeout=20)
        r.raise_for_status()
        return r.json().get("results", [])

    def get_transactions(
        self, access_token: str, account_id: str, from_date: date, to_date: date
    ) -> list[dict]:
        url = f"{self.cfg.api_base}/data/v1/accounts/{account_id}/transactions"
        params = {"from": from_date.isoformat(), "to": to_date.isoformat()}

        r = requests.get(
            url,
            headers=self._headers(access_token),
            params=params,
            timeout=30,
        )
        r.raise_for_status()
        return r.json().get("results", [])

    def get_direct_debits(self, access_token: str, account_id: str) -> list[dict]:
        url = f"{self.cfg.api_base}/data/v1/accounts/{account_id}/direct_debits"
        r = requests.get(url, headers=self._headers(access_token), timeout=20)
        r.raise_for_status()
        return r.json().get("results", [])

    def get_standing_orders(self, access_token: str, account_id: str) -> list[dict]:
        url = f"{self.cfg.api_base}/data/v1/accounts/{account_id}/standing_orders"
        r = requests.get(url, headers=self._headers(access_token), timeout=20)
        r.raise_for_status()
        return r.json().get("results", [])
