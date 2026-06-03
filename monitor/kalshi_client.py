from __future__ import annotations
import logging
from typing import Any
import requests

logger = logging.getLogger(__name__)

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"


class KalshiClient:
    def __init__(self, session: requests.Session | None = None, timeout: int = 15):
        self.session = session or requests.Session()
        self.timeout = timeout

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = self.session.get(f"{KALSHI_BASE}{path}", params=params, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            logger.warning(f"Kalshi GET {path} failed: {exc}")
            return {}

    def get_markets(
        self,
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        status: str = "open",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        markets: list[dict[str, Any]] = []
        cursor = None
        while True:
            params: dict[str, Any] = {"status": status, "limit": limit}
            if series_ticker:
                params["series_ticker"] = series_ticker
            if event_ticker:
                params["event_ticker"] = event_ticker
            if cursor:
                params["cursor"] = cursor

            data = self._get("/markets", params)
            batch = data.get("markets", [])
            if isinstance(batch, list):
                markets.extend(batch)
            cursor = data.get("cursor")
            if not cursor or not batch:
                break
        return markets

    def get_orderbook_asks(self, ticker: str) -> dict[str, list[dict[str, str]]]:
        data = self._get(f"/markets/{ticker}/orderbook")
        orderbook = data.get("orderbook", {}) or {}
        yes_bids = orderbook.get("yes")
        no_bids = orderbook.get("no")
        fp = data.get("orderbook_fp", {}) or {}

        if yes_bids is None and "yes_dollars" in fp:
            yes_bids = [[float(price), size] for price, size in fp.get("yes_dollars", [])]
            no_bids = [[float(price), size] for price, size in fp.get("no_dollars", [])]
            scale = 1.0
        else:
            yes_bids = yes_bids or []
            no_bids = no_bids or []
            scale = 0.01

        def bids_to_opposite_asks(bids: list) -> list[dict[str, str]]:
            asks = []
            for level in bids:
                try:
                    price = float(level[0]) * scale
                    size = float(level[1])
                except (IndexError, TypeError, ValueError):
                    continue

                ask_price = 1.0 - price
                if 0.0 < ask_price < 1.0 and size > 0:
                    asks.append({"price": f"{ask_price:.4f}", "size": str(size)})
            return sorted(asks, key=lambda ask: float(ask["price"]))

        return {
            "yes": bids_to_opposite_asks(no_bids),
            "no": bids_to_opposite_asks(yes_bids),
        }
