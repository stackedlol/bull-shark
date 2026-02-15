import logging
import time
import uuid
from decimal import Decimal

import requests

from src.coinbase.auth import build_jwt
from src.config import API_BASE

logger = logging.getLogger(__name__)


class CoinbaseAPIError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"HTTP {status_code}: {message}")


class CoinbaseClient:
    MAX_RETRIES = 5
    BACKOFF_BASE = 0.5

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.session = requests.Session()

    def _request(self, method: str, path: str, params: dict = None, json_body: dict = None):
        url = f"{API_BASE}{path}"
        for attempt in range(self.MAX_RETRIES):
            token = build_jwt(method, path)
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
            try:
                resp = self.session.request(
                    method, url, headers=headers, params=params, json=json_body, timeout=10
                )
            except requests.RequestException as e:
                logger.warning("Request failed (attempt %d): %s", attempt + 1, e)
                if attempt < self.MAX_RETRIES - 1:
                    time.sleep(self.BACKOFF_BASE * (2 ** attempt))
                    continue
                raise

            if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.MAX_RETRIES - 1:
                wait = self.BACKOFF_BASE * (2 ** attempt)
                logger.warning("HTTP %d, retrying in %.1fs (attempt %d)", resp.status_code, wait, attempt + 1)
                time.sleep(wait)
                continue

            if resp.status_code >= 400:
                raise CoinbaseAPIError(resp.status_code, resp.text)

            return resp.json()

        raise CoinbaseAPIError(0, "Max retries exceeded")

    # --- Read endpoints (always hit real API) ---

    def get_product(self, product_id: str) -> dict:
        return self._request("GET", f"/api/v3/brokerage/products/{product_id}")

    def get_best_bid_ask(self, product_ids: list[str]) -> dict:
        params = {"product_ids": ",".join(product_ids)}
        return self._request("GET", "/api/v3/brokerage/best_bid_ask", params=params)

    def get_candles(self, product_id: str, granularity: str = "ONE_HOUR", limit: int = 50) -> list:
        end = int(time.time())
        granularity_seconds = {
            "ONE_MINUTE": 60, "FIVE_MINUTE": 300, "FIFTEEN_MINUTE": 900,
            "THIRTY_MINUTE": 1800, "ONE_HOUR": 3600, "TWO_HOUR": 7200,
            "SIX_HOUR": 21600, "ONE_DAY": 86400,
        }
        seconds = granularity_seconds.get(granularity, 3600)
        start = end - (seconds * limit)
        params = {"start": str(start), "end": str(end), "granularity": granularity}
        resp = self._request("GET", f"/api/v3/brokerage/products/{product_id}/candles", params=params)
        return resp.get("candles", [])

    def get_accounts(self) -> list:
        resp = self._request("GET", "/api/v3/brokerage/accounts", params={"limit": "250"})
        return resp.get("accounts", [])

    def get_balance(self, currency: str) -> Decimal:
        accounts = self.get_accounts()
        for acct in accounts:
            if acct.get("currency") == currency:
                return Decimal(acct["available_balance"]["value"])
        return Decimal("0")

    def get_order(self, order_id: str) -> dict:
        return self._request("GET", f"/api/v3/brokerage/orders/historical/{order_id}")

    # --- Write endpoints (dry-run aware) ---

    def create_market_order(self, product_id: str, side: str, quote_size: str = None, base_size: str = None) -> dict:
        client_order_id = str(uuid.uuid4())
        order_config = {"market_market_ioc": {}}
        if quote_size:
            order_config["market_market_ioc"]["quote_size"] = quote_size
        if base_size:
            order_config["market_market_ioc"]["base_size"] = base_size

        body = {
            "client_order_id": client_order_id,
            "product_id": product_id,
            "side": side,
            "order_configuration": order_config,
        }

        if self.dry_run:
            logger.info("[DRY-RUN] Market %s %s | quote=%s base=%s", side, product_id, quote_size, base_size)
            return {
                "success": True,
                "order_id": f"dry-run-{client_order_id}",
                "success_response": {"order_id": f"dry-run-{client_order_id}"},
            }

        return self._request("POST", "/api/v3/brokerage/orders", json_body=body)

    def create_limit_order(
        self, product_id: str, side: str, base_size: str, limit_price: str, post_only: bool = True
    ) -> dict:
        client_order_id = str(uuid.uuid4())
        body = {
            "client_order_id": client_order_id,
            "product_id": product_id,
            "side": side,
            "order_configuration": {
                "limit_limit_gtc": {
                    "base_size": base_size,
                    "limit_price": limit_price,
                    "post_only": post_only,
                }
            },
        }

        if self.dry_run:
            logger.info(
                "[DRY-RUN] Limit %s %s | size=%s price=%s", side, product_id, base_size, limit_price
            )
            return {
                "success": True,
                "order_id": f"dry-run-{client_order_id}",
                "success_response": {"order_id": f"dry-run-{client_order_id}"},
            }

        return self._request("POST", "/api/v3/brokerage/orders", json_body=body)

    def cancel_orders(self, order_ids: list[str]) -> dict:
        if self.dry_run:
            logger.info("[DRY-RUN] Cancel orders: %s", order_ids)
            return {"results": [{"success": True, "order_id": oid} for oid in order_ids]}

        return self._request("POST", "/api/v3/brokerage/orders/batch_cancel", json_body={"order_ids": order_ids})
