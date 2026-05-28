from __future__ import annotations
import logging
from typing import Any
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from .config import Settings

logger = logging.getLogger(__name__)

class ApiClients:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.session = self._build_session()

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=3, connect=3, read=3, backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=frozenset(["GET", "POST"]),
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update({"User-Agent": "arb-bot/2.0"})
        return session

    def _get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        response = self.session.get(url, params=params, timeout=self.settings.request_timeout_seconds)
        response.raise_for_status()
        return response.json()

    # --- NBA METHODS ---
    def get_fiat_data(self) -> list[dict[str, Any]]:
        url = "https://api.the-odds-api.com/v4/sports/basketball_nba/odds"
        params = {
            "apiKey": self.settings.odds_api_key,
            "regions": "eu,us",
            "markets": "h2h,totals,spreads",
            "bookmakers": "pinnacle,onexbet,draftkings",
        }
        try:
            data = self._get_json(url, params=params)
            return data if isinstance(data, list) else []
        except Exception as exc:
            logger.error(f"Odds API request failed: {exc}")
            return []

    def get_polymarket_events(self) -> list[dict[str, Any]]:
        url = "https://gamma-api.polymarket.com/events"
        params = {"series_id": 10345, "active": "true", "closed": "false", "limit": 100}
        try:
            data = self._get_json(url, params=params)
            if isinstance(data, list): return data
            if isinstance(data, dict): return data.get("events", [])
            return []
        except Exception as exc:
            logger.error(f"Polymarket request failed: {exc}")
            return []

    # --- SHARED POLYMARKET CLOB METHOD ---
    def get_clob_book(self, token_id: str) -> dict[str, Any]:
        if not str(token_id).strip(): return {"asks": [], "bids": [], "timestamp": "0"}
        
        # REVERTED: Back to the stable V1 structure that perfectly handles the data
        url = "https://clob.polymarket.com/book"
        params = {"token_id": token_id}
        
        try:
            data = self._get_json(url, params=params)
            if not isinstance(data, dict): return {"asks": [], "bids": [], "timestamp": "0"}
            return {
                "asks": data.get("asks", []),
                "bids": data.get("bids", []),
                "timestamp": data.get("timestamp", "0")
            }
        except Exception as exc:
            logger.warning(f"CLOB request failed for token {token_id}: {exc}")
            return {"asks": [], "bids": [], "timestamp": "0"}

    # --- SHARED TELEGRAM SENDER ---
    def send_telegram_alert(self, message: str) -> bool:
        if not message.strip(): return False
        url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage"
        payload = {"chat_id": self.settings.telegram_chat_id, "text": message}
        try:
            response = self.session.post(url, json=payload, timeout=self.settings.request_timeout_seconds)
            response.raise_for_status()
            return True
        except Exception as exc:
            logger.error(f"Telegram send failed: {exc}")
            return False

    def close(self) -> None:
        self.session.close()

    # --- MMA / UFC METHODS ---
    def get_mma_fiat_data(self) -> list[dict[str, Any]]:
        url = "https://api.the-odds-api.com/v4/sports/mma_mixed_martial_arts/odds"
        params = {
            "apiKey": self.settings.odds_api_key,
            "regions": "eu,us",
            "markets": "h2h,totals", 
            "bookmakers": "pinnacle,onexbet,draftkings",
        }
        try:
            data = self._get_json(url, params=params)
            return data if isinstance(data, list) else []
        except Exception as exc:
            logger.error(f"MMA Odds API request failed: {exc}")
            return []

    def get_mma_polymarket_events(self) -> list[dict[str, Any]]:
        url = "https://gamma-api.polymarket.com/events"
        all_events = []
        for offset in range(0, 5000, 100):
            params = {"active": "true", "closed": "false", "limit": 100, "offset": offset}
            try:
                data = self._get_json(url, params=params)
                if isinstance(data, list): 
                    all_events.extend(data)
                    if len(data) < 100: break
                elif isinstance(data, dict): 
                    events = data.get("events", [])
                    all_events.extend(events)
                    if len(events) < 100: break
                else: break
            except Exception as exc:
                logger.error(f"MMA Polymarket pagination failed at offset {offset}: {exc}")
                break
        return all_events

    # --- SOCCER / FOOTBALL METHODS ---
    def get_soccer_fiat_data(self) -> list[dict[str, Any]]:
        league = "soccer_fifa_world_cup"
        url = f"https://api.the-odds-api.com/v4/sports/{league}/odds"
        params = {
            "apiKey": self.settings.odds_api_key,
            "regions": "eu,us",
            "markets": "h2h,totals,btts",
            "bookmakers": "pinnacle,onexbet,draftkings",
        }
        try:
            data = self._get_json(url, params=params)
            if isinstance(data, list):
                return data
        except requests.exceptions.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                logger.info(f"   [INFO] ⚽ {league} is currently inactive (404). Skipping safely...")
            else:
                logger.error(f"Soccer Odds API request failed for {league}: {exc}")
        except Exception as exc:
            logger.error(f"Soccer Odds API request failed for {league}: {exc}")
        return []

    def get_soccer_polymarket_events(self) -> list[dict[str, Any]]:
        url = "https://gamma-api.polymarket.com/events"
        all_events = []
        for offset in range(0, 5000, 100):
            params = {"active": "true", "closed": "false", "limit": 100, "offset": offset}
            try:
                data = self._get_json(url, params=params)
                if isinstance(data, list): 
                    all_events.extend(data)
                    if len(data) < 100: break
                elif isinstance(data, dict): 
                    events = data.get("events", [])
                    all_events.extend(events)
                    if len(events) < 100: break
                else: break
            except Exception as exc:
                logger.error(f"Soccer Polymarket pagination failed at offset {offset}: {exc}")
                break
        return all_events
