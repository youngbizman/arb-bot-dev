from __future__ import annotations
import logging
import re
from datetime import datetime, timezone
from typing import Any
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from .config import Settings

logger = logging.getLogger(__name__)

POPULAR_TENNIS_SPORT_KEYS = (
    "tennis_atp_aus_open_singles",
    "tennis_atp_french_open",
    "tennis_atp_wimbledon",
    "tennis_atp_us_open",
    "tennis_atp_indian_wells",
    "tennis_atp_miami_open",
    "tennis_atp_monte_carlo_masters",
    "tennis_atp_madrid_open",
    "tennis_atp_italian_open",
    "tennis_atp_canadian_open",
    "tennis_atp_cincinnati_open",
    "tennis_atp_shanghai_masters",
    "tennis_atp_paris_masters",
    "tennis_atp_barcelona_open",
    "tennis_atp_hamburg_open",
    "tennis_atp_dubai",
    "tennis_atp_qatar_open",
    "tennis_atp_munich",
    "tennis_atp_china_open",
    "tennis_wta_aus_open_singles",
    "tennis_wta_french_open",
    "tennis_wta_wimbledon",
    "tennis_wta_us_open",
    "tennis_wta_indian_wells",
    "tennis_wta_miami_open",
    "tennis_wta_madrid_open",
    "tennis_wta_italian_open",
    "tennis_wta_canadian_open",
    "tennis_wta_cincinnati_open",
    "tennis_wta_dubai",
    "tennis_wta_qatar_open",
    "tennis_wta_china_open",
    "tennis_wta_wuhan_open",
    "tennis_wta_charleston_open",
    "tennis_wta_strasbourg",
    "tennis_wta_stuttgart_open",
)

POPULAR_BASEBALL_SPORT_KEYS = (
    "baseball_mlb",
)

API_FOOTBALL_BASE_URL = "https://v3.football.api-sports.io"
API_FOOTBALL_FRIENDLIES_LEAGUE_ID = 10
API_FOOTBALL_BOOKMAKERS = {
    4: "Pinnacle",
    11: "1xBet",
}
API_FOOTBALL_SOCCER_BETS = {
    1: "h2h",
    5: "totals",
    8: "btts",
}

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
            "bookmakers": "pinnacle,onexbet",
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
            "bookmakers": "pinnacle,onexbet",
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

    # --- TENNIS METHODS ---
    def _get_active_tennis_sport_keys(self) -> set[str]:
        url = "https://api.the-odds-api.com/v4/sports"
        params = {"apiKey": self.settings.odds_api_key}
        try:
            data = self._get_json(url, params=params)
            if isinstance(data, list):
                return {str(row.get("key")) for row in data if str(row.get("key", "")).startswith("tennis_")}
        except Exception as exc:
            logger.warning(f"Tennis sports list request failed: {exc}")
        return set()

    def get_tennis_fiat_data(self) -> list[dict[str, Any]]:
        active_keys = self._get_active_tennis_sport_keys()
        sport_keys = [key for key in POPULAR_TENNIS_SPORT_KEYS if not active_keys or key in active_keys]
        all_events: list[dict[str, Any]] = []

        for sport_key in sport_keys:
            url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds"
            params = {
                "apiKey": self.settings.odds_api_key,
                "regions": "eu,us",
                "markets": "h2h,totals,spreads",
                "bookmakers": "pinnacle,onexbet",
                "oddsFormat": "decimal",
            }
            try:
                data = self._get_json(url, params=params)
                if isinstance(data, list):
                    all_events.extend(data)
                    logger.info(f"   [INFO] Tennis Odds API {sport_key}: {len(data)} events.")
            except requests.exceptions.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 404:
                    logger.info(f"   [INFO] Tennis league {sport_key} is inactive (404). Skipping...")
                else:
                    logger.error(f"Tennis Odds API request failed for {sport_key}: {exc}")
            except Exception as exc:
                logger.error(f"Tennis Odds API request failed for {sport_key}: {exc}")
        return all_events

    def get_tennis_polymarket_events(self) -> list[dict[str, Any]]:
        url = "https://gamma-api.polymarket.com/events"
        all_events = []
        for offset in range(0, 5000, 100):
            params = {"tag_id": 864, "active": "true", "closed": "false", "limit": 100, "offset": offset}
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
                logger.error(f"Tennis Polymarket pagination failed at offset {offset}: {exc}")
                break
        return all_events

    # --- BASEBALL METHODS ---
    def _get_active_baseball_sport_keys(self) -> set[str]:
        url = "https://api.the-odds-api.com/v4/sports"
        params = {"apiKey": self.settings.odds_api_key}
        try:
            data = self._get_json(url, params=params)
            if isinstance(data, list):
                return {str(row.get("key")) for row in data if str(row.get("key", "")).startswith("baseball_")}
        except Exception as exc:
            logger.warning(f"Baseball sports list request failed: {exc}")
        return set()

    def get_baseball_fiat_data(self) -> list[dict[str, Any]]:
        active_keys = self._get_active_baseball_sport_keys()
        sport_keys = [key for key in POPULAR_BASEBALL_SPORT_KEYS if not active_keys or key in active_keys]
        all_events: list[dict[str, Any]] = []

        for sport_key in sport_keys:
            url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds"
            params = {
                "apiKey": self.settings.odds_api_key,
                "regions": "eu,us",
                "markets": "h2h,totals,spreads",
                "bookmakers": "pinnacle,onexbet",
                "oddsFormat": "decimal",
            }
            try:
                data = self._get_json(url, params=params)
                if isinstance(data, list):
                    all_events.extend(data)
                    logger.info(f"   [INFO] Baseball Odds API {sport_key}: {len(data)} events.")
            except requests.exceptions.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 404:
                    logger.info(f"   [INFO] Baseball league {sport_key} is inactive (404). Skipping...")
                else:
                    logger.error(f"Baseball Odds API request failed for {sport_key}: {exc}")
            except Exception as exc:
                logger.error(f"Baseball Odds API request failed for {sport_key}: {exc}")
        return all_events

    def get_baseball_polymarket_events(self) -> list[dict[str, Any]]:
        url = "https://gamma-api.polymarket.com/events"
        all_events = []
        seen: set[str] = set()
        sources = (("series_id", 3), ("tag_id", 102668))

        for source_key, source_value in sources:
            for offset in range(0, 5000, 100):
                params = {
                    source_key: source_value,
                    "active": "true",
                    "closed": "false",
                    "limit": 100,
                    "offset": offset,
                }
                try:
                    data = self._get_json(url, params=params)
                    if isinstance(data, list):
                        events = data
                    elif isinstance(data, dict):
                        events = data.get("events", [])
                    else:
                        break

                    for event in events:
                        event_key = str(event.get("id") or event.get("slug") or event.get("title"))
                        if event_key not in seen:
                            seen.add(event_key)
                            all_events.append(event)

                    if len(events) < 100:
                        break
                except Exception as exc:
                    logger.error(f"Baseball Polymarket pagination failed for {source_key}={source_value} at offset {offset}: {exc}")
                    break
        return all_events

    # --- SOCCER / FOOTBALL METHODS ---
    def get_soccer_fiat_data(self) -> list[dict[str, Any]]:
        events = self._get_soccer_world_cup_odds()
        events.extend(self.get_soccer_friendlies_fiat_data())
        return events

    def _get_soccer_world_cup_odds(self) -> list[dict[str, Any]]:
        league = "soccer_fifa_world_cup"
        url = f"https://api.the-odds-api.com/v4/sports/{league}/odds"
        params = {
            "apiKey": self.settings.odds_api_key,
            "markets": "h2h,totals",
            "bookmakers": "pinnacle,onexbet",
            "oddsFormat": "decimal",
        }
        try:
            data = self._get_json(url, params=params)
            if isinstance(data, list):
                for event in data:
                    event_id = event.get("id")
                    if not event_id:
                        continue
                    event_odds = self._get_soccer_event_odds(league, event_id, "btts")
                    self._merge_event_markets(event, event_odds, {"btts"})
                return data
        except requests.exceptions.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                logger.info(f"   [INFO] ⚽ {league} is currently inactive (404). Skipping safely...")
            else:
                logger.error(f"Soccer Odds API request failed for {league}: {exc}")
        except Exception as exc:
            logger.error(f"Soccer Odds API request failed for {league}: {exc}")
        return []

    def get_soccer_friendlies_fiat_data(self) -> list[dict[str, Any]]:
        if not self.settings.api_football_key:
            logger.info("   [INFO] API-Football Friendlies disabled: missing API_FOOTBALL_KEY.")
            return []

        season = datetime.now(timezone.utc).year
        raw_events = self._get_api_football_friendlies_odds(season)
        if not raw_events:
            return []

        fixture_details = self._get_api_football_fixtures_by_ids(raw_events.keys())
        converted_events: list[dict[str, Any]] = []

        for fixture_id, raw_event in raw_events.items():
            fixture_detail = fixture_details.get(fixture_id)
            if not fixture_detail:
                continue

            fixture = fixture_detail.get("fixture", {})
            status = fixture.get("status", {})
            if status.get("short") not in {"NS", "TBD"}:
                continue

            teams = fixture_detail.get("teams", {})
            home_team = teams.get("home", {}).get("name")
            away_team = teams.get("away", {}).get("name")
            if not home_team or not away_team:
                continue

            event = {
                "id": f"api-football-{fixture_id}",
                "sport_key": "soccer_friendlies",
                "sport_title": "Friendlies",
                "commence_time": fixture.get("date") or raw_event.get("commence_time"),
                "home_team": home_team,
                "away_team": away_team,
                "bookmakers": [],
            }

            for bookmaker_id, raw_bookmaker in raw_event.get("bookmakers", {}).items():
                markets = []
                for bet_id, values in raw_bookmaker.get("bets", {}).items():
                    market = self._api_football_bet_to_market(bet_id, values, home_team, away_team)
                    if market:
                        markets.append(market)

                if markets:
                    event["bookmakers"].append({
                        "key": f"api_football_{bookmaker_id}",
                        "title": raw_bookmaker.get("name"),
                        "last_update": raw_bookmaker.get("last_update"),
                        "markets": markets,
                    })

            if event["bookmakers"]:
                converted_events.append(event)

        logger.info(f"   [INFO] API-Football Friendlies: {len(converted_events)} events.")
        return converted_events

    def _get_api_football_friendlies_odds(self, season: int) -> dict[str, dict[str, Any]]:
        by_fixture: dict[str, dict[str, Any]] = {}

        for bookmaker_id, bookmaker_name in API_FOOTBALL_BOOKMAKERS.items():
            for bet_id in API_FOOTBALL_SOCCER_BETS:
                page = 1

                while True:
                    payload = self._get_api_football_json("odds", {
                        "league": API_FOOTBALL_FRIENDLIES_LEAGUE_ID,
                        "season": season,
                        "bookmaker": bookmaker_id,
                        "bet": bet_id,
                        "page": page,
                    })
                    error_message = self._api_football_error_message(payload)
                    if error_message:
                        logger.info(
                            f"   [INFO] API-Football Friendlies unavailable "
                            f"({bookmaker_name}, bet {bet_id}): {error_message}"
                        )
                        if "plan" in error_message.lower():
                            return {}
                        break

                    for row in payload.get("response", []):
                        fixture_id = str(row.get("fixture", {}).get("id", "")).strip()
                        if not fixture_id:
                            continue

                        raw_event = by_fixture.setdefault(fixture_id, {
                            "commence_time": row.get("fixture", {}).get("date"),
                            "bookmakers": {},
                        })
                        raw_bookmaker = raw_event["bookmakers"].setdefault(bookmaker_id, {
                            "name": bookmaker_name,
                            "last_update": row.get("update"),
                            "bets": {},
                        })
                        raw_bookmaker["last_update"] = row.get("update") or raw_bookmaker.get("last_update")

                        for bookmaker in row.get("bookmakers", []):
                            for bet in bookmaker.get("bets", []):
                                if bet.get("id") == bet_id:
                                    raw_bookmaker["bets"][bet_id] = bet.get("values", [])

                    paging = payload.get("paging", {})
                    total_pages = int(paging.get("total") or 1)
                    if page >= total_pages:
                        break
                    page += 1

        return by_fixture

    def _get_api_football_fixtures_by_ids(self, fixture_ids: Any) -> dict[str, dict[str, Any]]:
        fixture_id_list = list(dict.fromkeys(str(fixture_id) for fixture_id in fixture_ids))
        fixtures: dict[str, dict[str, Any]] = {}

        for index in range(0, len(fixture_id_list), 20):
            ids_param = "-".join(fixture_id_list[index:index + 20])
            payload = self._get_api_football_json("fixtures", {"ids": ids_param})
            error_message = self._api_football_error_message(payload)
            if error_message:
                logger.info(f"   [INFO] API-Football fixture lookup unavailable: {error_message}")
                continue

            for row in payload.get("response", []):
                fixture_id = str(row.get("fixture", {}).get("id", "")).strip()
                if fixture_id:
                    fixtures[fixture_id] = row

        return fixtures

    def _api_football_bet_to_market(
        self,
        bet_id: int,
        values: list[dict[str, Any]],
        home_team: str,
        away_team: str,
    ) -> dict[str, Any] | None:
        outcomes = []

        if bet_id == 1:
            for value in values:
                selection = str(value.get("value", "")).strip().lower()
                if selection == "home":
                    name = home_team
                elif selection == "away":
                    name = away_team
                elif selection == "draw":
                    name = "Draw"
                else:
                    continue

                odd = value.get("odd")
                if odd is not None:
                    outcomes.append({"name": name, "price": str(odd)})

            return {"key": "h2h", "outcomes": outcomes} if outcomes else None

        if bet_id == 5:
            for value in values:
                line_match = re.match(r"^(over|under)\s+(\d+(?:\.\d+)?)$", str(value.get("value", "")).strip(), re.I)
                if not line_match:
                    continue

                odd = value.get("odd")
                if odd is not None:
                    outcomes.append({
                        "name": line_match.group(1).title(),
                        "price": str(odd),
                        "point": float(line_match.group(2)),
                    })

            return {"key": "totals", "outcomes": outcomes} if outcomes else None

        if bet_id == 8:
            for value in values:
                selection = str(value.get("value", "")).strip().title()
                if selection not in {"Yes", "No"}:
                    continue

                odd = value.get("odd")
                if odd is not None:
                    outcomes.append({"name": selection, "price": str(odd)})

            return {"key": "btts", "outcomes": outcomes} if outcomes else None

        return None

    def _get_api_football_json(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{API_FOOTBALL_BASE_URL}/{endpoint.lstrip('/')}"
        try:
            response = self.session.get(
                url,
                params=params,
                headers={"x-apisports-key": self.settings.api_football_key or ""},
                timeout=self.settings.request_timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            logger.error(f"API-Football request failed for {endpoint}: {exc}")
            return {}

    def _api_football_error_message(self, payload: dict[str, Any]) -> str:
        errors = payload.get("errors")
        if not errors:
            return ""
        if isinstance(errors, dict):
            return "; ".join(f"{key}: {value}" for key, value in errors.items())
        if isinstance(errors, list):
            return "; ".join(str(value) for value in errors)
        return str(errors)

    def _get_soccer_event_odds(self, league: str, event_id: str, markets: str) -> dict[str, Any]:
        url = f"https://api.the-odds-api.com/v4/sports/{league}/events/{event_id}/odds"
        params = {
            "apiKey": self.settings.odds_api_key,
            "markets": markets,
            "bookmakers": "pinnacle,onexbet",
            "oddsFormat": "decimal",
        }
        try:
            data = self._get_json(url, params=params)
            return data if isinstance(data, dict) else {}
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "unknown"
            logger.info(f"   [INFO] ⚽ Event odds unavailable for {event_id} ({markets}, HTTP {status}). Skipping...")
        except Exception as exc:
            logger.error(f"Soccer event odds request failed for {event_id} ({markets}): {exc}")
        return {}

    def _merge_event_markets(
        self,
        base_event: dict[str, Any],
        event_odds: dict[str, Any],
        market_keys: set[str],
    ) -> None:
        base_bookmakers = base_event.setdefault("bookmakers", [])
        by_key = {b.get("key"): b for b in base_bookmakers if b.get("key")}

        for event_bookmaker in event_odds.get("bookmakers", []):
            markets = [m for m in event_bookmaker.get("markets", []) if m.get("key") in market_keys]
            if not markets:
                continue

            bookmaker_key = event_bookmaker.get("key")
            target = by_key.get(bookmaker_key)
            if target is None:
                target = {
                    "key": bookmaker_key,
                    "title": event_bookmaker.get("title"),
                    "last_update": event_bookmaker.get("last_update"),
                    "markets": [],
                }
                base_bookmakers.append(target)
                if bookmaker_key:
                    by_key[bookmaker_key] = target

            target["markets"] = [
                market for market in target.get("markets", []) if market.get("key") not in market_keys
            ]
            target["markets"].extend(markets)

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
