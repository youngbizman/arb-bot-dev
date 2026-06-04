import logging
import json
import unicodedata
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional
from datetime import datetime, timedelta, timezone
from decimal import Decimal, getcontext
from zoneinfo import ZoneInfo
from thefuzz import fuzz

from .api_clients import ApiClients
from .config import ConfigError, load_settings
from .models import ArbitrageOpportunity, FiatArbitrageOpportunity
from .alerts import build_soccer_global_alerts
from .arb_core import (
    Leg,
    fiat_fiat_legs_from_books,
    format_nway_alert,
    q_from_decimal,
    q_from_polymarket,
    solve_nway,
)
from .kalshi_client import KalshiClient

logger = logging.getLogger(__name__)
getcontext().prec = 28
SOCCER_MAX_ROI = 15.0
ALLOWED_FIAT_BOOKMAKER_KEYS = {"pinnacle", "onexbet"}
EMPTY_CLOB_BOOK = {"asks": [], "bids": [], "timestamp": "0"}

@dataclass(frozen=True)
class BookLevel:
    price: Decimal
    size: Decimal

@dataclass
class HedgeEstimate:
    best_ask: Optional[Decimal]
    shares: Decimal
    sportsbook_stake: Decimal
    poly_spend: Decimal
    poly_fees: Decimal
    total_outlay: Decimal
    vwap: Optional[Decimal]
    marginal_price: Optional[Decimal]
    locked_profit: Decimal
    passes_liquidity_filter: bool
    reject_reason: Optional[str]

def normalize_asks(asks: Iterable[Mapping[str, str]]) -> list[BookLevel]:
    levels: list[BookLevel] = []
    for row in asks:
        try:
            p, s = Decimal(str(row.get("price", "0"))), Decimal(str(row.get("size", "0")))
            if s > 0: levels.append(BookLevel(price=p, size=s))
        except: pass
    return sorted(levels, key=lambda lvl: lvl.price)

def fee_per_share(p: Decimal, r: Decimal) -> Decimal:
    return r * p * (Decimal("1") - p)

# RESTORED: Fee rate set back to exactly 3% (0.03) to match Polymarket reality
def evaluate_buy_hedge_from_asks(asks, decimal_odds, bankroll="100", fee_rate="0.03", max_avg_impact_rel="0.02"):
    levels = normalize_asks(asks)
    odds, bankroll_d, fee_r = Decimal(str(decimal_odds)), Decimal(bankroll), Decimal(fee_rate)
    inv_odds = Decimal("1") / odds
    eps = Decimal("0.0000000001")

    if not levels: return HedgeEstimate(None, Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), None, None, Decimal("0"), False, "Empty Orderbook")

    best = levels[0]
    if best.price <= 0: return HedgeEstimate(best.price, Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), None, None, Decimal("0"), False, "Invalid Price")

    q, cost, fees = Decimal("0"), Decimal("0"), Decimal("0")
    marginal, full_bankroll_supported = None, False

    for lvl in levels:
        lvl_fee_ps = fee_per_share(lvl.price, fee_r)
        lvl_all_in_ps = lvl.price + lvl_fee_ps + inv_odds
        if lvl_all_in_ps >= Decimal("1"): break
        rem = bankroll_d - ((q * inv_odds) + cost + fees)
        if rem <= eps: 
            full_bankroll_supported = True
            break
        affordable = rem / lvl_all_in_ps
        take = min(lvl.size, affordable)
        if take <= 0: break
        q += take
        cost += take * lvl.price
        fees += take * lvl_fee_ps
        marginal = lvl.price
        if take < lvl.size:
            full_bankroll_supported = True
            break

    total = cost + fees + (q * inv_odds)
    if total >= bankroll_d - eps: full_bankroll_supported = True
    if q <= Decimal("0"): return HedgeEstimate(best.price, Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), None, None, Decimal("0"), False, "No profitable depth")

    vwap = cost / q
    profit = q - total
    impact = (vwap / best.price) - Decimal("1")
    reason = None
    if not full_bankroll_supported: reason = "Insufficient depth for $100 bankroll"
    elif impact > Decimal(max_avg_impact_rel): reason = "Slippage exceeds 2% buffer"
    elif profit <= 0: reason = "Negative profit after fees"

    return HedgeEstimate(best.price, q, (q/odds), cost, fees, total, vwap, marginal, profit, (reason is None), reason)

def clean_for_matching(text: str) -> str:
    if not text: return ""
    text = unicodedata.normalize('NFKD', str(text)).encode('ASCII', 'ignore').decode('utf-8').lower()
    text = text.replace('-', ' ')
    return re.sub(r'[^a-z0-9\s]', '', text)

def parse_json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []

def collect_polymarket_token_ids(event: dict) -> list[str]:
    token_ids: list[str] = []
    seen: set[str] = set()
    for market in event.get("markets", []):
        if not market.get("acceptingOrders"):
            continue
        for token_id in parse_json_list(market.get("clobTokenIds")):
            key = str(token_id).strip()
            if key and key not in seen:
                seen.add(key)
                token_ids.append(key)
    return token_ids

def prefetch_polymarket_books(clients: ApiClients, event: dict) -> dict[str, dict[str, Any]]:
    token_ids = collect_polymarket_token_ids(event)
    if not token_ids:
        return {}
    return clients.get_clob_books(token_ids)

def get_prefetched_poly_book(poly_books: dict[str, dict[str, Any]], token_id: str) -> dict[str, Any]:
    return poly_books.get(str(token_id).strip(), EMPTY_CLOB_BOOK)

def is_team_match(fiat_team: str, poly_text: str) -> bool:
    if not poly_text: return False
    nicknames = {
        "paris saint germain": "psg",
        "manchester city": "man city",
        "manchester united": "man utd",
        "atletico madrid": "atletico",
        "tottenham hotspur": "spurs",
        "bayern munich": "bayern",
        "bayern munchen": "bayern",  
        "borussia dortmund": "dortmund",
        "ac milan": "milan",
        "internazionale": "inter"
    }
    f_str = clean_for_matching(fiat_team)
    p_str = clean_for_matching(poly_text)
    for full, short in nicknames.items():
        if full in f_str: f_str = f_str.replace(full, short)
        if full in p_str: p_str = p_str.replace(full, short)
    return fuzz.token_set_ratio(f_str, p_str) > 75 

def format_to_local(iso: str) -> str:
    try: return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ZoneInfo("America/Toronto")).strftime("%Y-%m-%d %I:%M %p")
    except: return iso[:10]

def normalize_double_chance_outcome(name: str, home: str, away: str) -> Optional[str]:
    raw = clean_for_matching(name)
    if raw in {"1x", "home draw", "home or draw", "draw home", "draw or home"}:
        return "1X"
    if raw in {"x2", "draw away", "draw or away", "away draw", "away or draw"}:
        return "X2"
    if raw in {"12", "home away", "home or away", "away home", "away or home"}:
        return "12"

    has_home = "home" in raw or is_team_match(home, name)
    has_away = "away" in raw or is_team_match(away, name)
    has_draw = "draw" in raw or raw in {"x"}
    if has_home and has_draw and not has_away:
        return "1X"
    if has_away and has_draw and not has_home:
        return "X2"
    if has_home and has_away and not has_draw:
        return "12"
    return None

def _parse_dt(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None

def _poly_event_dt(event: dict) -> Optional[datetime]:
    for key in ("startDate", "endDate", "gameStartTime"):
        dt = _parse_dt(event.get(key))
        if dt:
            return dt
    for market in event.get("markets", []):
        dt = _parse_dt(market.get("gameStartTime"))
        if dt:
            return dt
    return None

def is_same_fixture_time(fiat_time: str, poly_event: dict, max_hours: int = 36) -> bool:
    fiat_dt = _parse_dt(fiat_time)
    poly_dt = _poly_event_dt(poly_event)
    if not fiat_dt or not poly_dt:
        return True
    return abs((poly_dt - fiat_dt).total_seconds()) <= max_hours * 3600

def parse_poly_total_market(question: str) -> tuple[Optional[float], Optional[str]]:
    text = str(question).lower()
    more_match = re.search(r'(\d+)\s*(?:\+|or more)\s*goals?', text)
    if more_match:
        return float(int(more_match.group(1)) - 0.5), "over"

    over_match = re.search(r'(?:over|more than)\s*(\d+(?:\.\d+)?)\s*goals?', text)
    if over_match:
        return float(over_match.group(1)), "over"

    under_match = re.search(r'(?:under|fewer than|less than)\s*(\d+(?:\.\d+)?)\s*goals?', text)
    if under_match:
        return float(under_match.group(1)), "under"

    ou_match = re.search(r'(?:o/u|over/under)\s*(\d+(?:\.\d+)?)', text)
    if ou_match:
        return float(ou_match.group(1)), None

    half_line = re.search(r'(\d+\.5)', text)
    if half_line and "goal" in text:
        return float(half_line.group(1)), None

    return None, None

def poly_side_from_outcome(outcome: str, yes_side: Optional[str]) -> Optional[str]:
    label = str(outcome).strip().lower()
    if label.startswith("over"):
        return "over"
    if label.startswith("under"):
        return "under"
    if label == "yes" and yes_side in {"over", "under"}:
        return yes_side
    if label == "no" and yes_side == "over":
        return "under"
    if label == "no" and yes_side == "under":
        return "over"
    return None

def canonical_team_from_label(label: str, home: str, away: str) -> Optional[str]:
    raw = clean_for_matching(label)
    if raw == "home":
        return home
    if raw == "away":
        return away
    if is_team_match(home, label):
        return home
    if is_team_match(away, label):
        return away
    return None

def get_h2h_odds(bookie: dict, selection: str) -> Optional[Decimal]:
    for name, odds in bookie.get("h2h", {}).items():
        if selection == name or is_team_match(selection, str(name)) or is_team_match(str(name), selection):
            return odds
    return None

def get_draw_odds(bookie: dict) -> Optional[Decimal]:
    for name, odds in bookie.get("h2h", {}).items():
        if str(name).lower() == "draw":
            return odds
    return None

def get_double_chance_for_team_not_to_win(bookie: dict, team: str, home: str, away: str) -> tuple[Optional[Decimal], str]:
    code = "X2" if team == home else "1X"
    return bookie.get("double_chance", {}).get(code), code

def get_synthetic_double_chance_against_team(bookie: dict, team: str, home: str, away: str) -> tuple[Optional[Decimal], str]:
    opponent = away if team == home else home
    opp_odds, draw_odds = get_h2h_odds(bookie, opponent), get_draw_odds(bookie)
    if not opp_odds or not draw_odds:
        return None, ""
    return Decimal("1") / ((Decimal("1") / opp_odds) + (Decimal("1") / draw_odds)), f"Draw or {opponent}"

def get_no_draw_odds(bookie: dict, home: str, away: str) -> tuple[Optional[Decimal], str]:
    direct = bookie.get("double_chance", {}).get("12")
    if direct:
        return direct, "Home or Away"
    home_odds, away_odds = get_h2h_odds(bookie, home), get_h2h_odds(bookie, away)
    if not home_odds or not away_odds:
        return None, ""
    return Decimal("1") / ((Decimal("1") / home_odds) + (Decimal("1") / away_odds)), f"{home} or {away}"

def get_spread_odds(bookie: dict, team: str, point: float) -> Optional[Decimal]:
    target = round(float(point), 3)
    for raw_point, sides in bookie.get("spreads", {}).items():
        if round(float(raw_point), 3) != target:
            continue
        for selection, odds in sides.items():
            if selection == team or is_team_match(team, str(selection)) or is_team_match(str(selection), team):
                return odds
    return None

def get_team_total_odds(bookie: dict, team: str, line: float, side: str) -> Optional[Decimal]:
    target = round(float(line), 3)
    side_key = side.lower()
    for team_name, lines in bookie.get("team_totals", {}).items():
        if not (team_name == team or is_team_match(team, str(team_name)) or is_team_match(str(team_name), team)):
            continue
        for raw_line, sides in lines.items():
            if round(float(raw_line), 3) == target:
                return sides.get(side_key)
    return None

def team_from_win_question(question: str, home: str, away: str) -> Optional[str]:
    text = str(question).lower()
    if "draw" in text and "win" not in text:
        return "Draw"
    if is_team_match(home, text):
        return home
    if is_team_match(away, text):
        return away
    return None

def matching_kalshi_markets(markets: list[dict], home: str, away: str) -> list[dict]:
    matches = []
    for market in markets:
        text = " ".join(str(market.get(key, "")) for key in ("title", "subtitle", "yes_sub_title", "no_sub_title", "rules_primary", "ticker"))
        if is_team_match(home, text) and is_team_match(away, text):
            matches.append(market)
    return matches

def is_draw_market(question: str) -> bool:
    text = str(question).lower()
    return "draw" in text and "draw no bet" not in text and "group" not in text

def parse_poly_team_total_market(question: str, home: str, away: str) -> tuple[Optional[str], Optional[float], Optional[str]]:
    text = str(question).lower()
    if "goal" not in text:
        return None, None, None
    if "win by" in text or "spread" in text or "handicap" in text or "cover" in text:
        return None, None, None
    if home and away and is_team_match(home, question) and is_team_match(away, question):
        return None, None, None
    team = canonical_team_from_label(question, home, away)
    if not team:
        return None, None, None

    more_match = re.search(r'(\d+)\s*(?:\+|or more)\s*goals?', text)
    if more_match:
        return team, float(int(more_match.group(1)) - 0.5), "over"

    over_match = re.search(r'(?:over|more than)\s*(\d+(?:\.\d+)?)\s*goals?', text)
    if over_match:
        return team, float(over_match.group(1)), "over"

    under_match = re.search(r'(?:under|fewer than|less than)\s*(\d+(?:\.\d+)?)\s*goals?', text)
    if under_match:
        return team, float(under_match.group(1)), "under"

    half_line = re.search(r'(\d+\.5)', text)
    if half_line:
        return team, float(half_line.group(1)), None

    return None, None, None

def parse_poly_spread_market(question: str, home: str, away: str) -> tuple[Optional[str], Optional[float]]:
    text = str(question).lower()
    team = canonical_team_from_label(question, home, away)
    if not team:
        return None, None

    explicit = re.search(r'([+-]\d+(?:\.\d+)?)', text)
    if explicit:
        return team, float(explicit.group(1))

    win_by = re.search(r'win by\s*(\d+)\s*(?:\+|or more)?', text)
    if win_by:
        return team, -(float(win_by.group(1)) - 0.5)

    return None, None

def poly_ml_outcome_from_question(question: str, home: str, away: str) -> Optional[str]:
    text = str(question).lower()
    if is_draw_market(text):
        return "Draw"
    if "win" not in text and "beat" not in text:
        return None
    team = canonical_team_from_label(question, home, away)
    if team == home:
        return "Home"
    if team == away:
        return "Away"
    return None

def best_poly_leg_from_token(poly_books: dict[str, dict[str, Any]], token_id: str, outcome: str, venue: str = "Polymarket") -> Optional[Leg]:
    book = get_prefetched_poly_book(poly_books, token_id)
    levels = normalize_asks(book.get("asks", []))
    if not levels:
        return None
    best = levels[0]
    if best.price <= 0 or best.price >= 1 or best.size <= 0:
        return None
    return Leg(outcome, venue, q_from_polymarket(best.price), best.price, best.size)

def fiat_ml_legs(game: dict, bookies: list[dict]) -> list[Leg]:
    best: dict[str, tuple[Decimal, str]] = {}
    for book in bookies:
        for selection, odds in book.get("h2h", {}).items():
            if selection == game["home"]:
                key = "Home"
            elif selection == game["away"]:
                key = "Away"
            elif str(selection).lower() == "draw":
                key = "Draw"
            else:
                continue
            if key not in best or odds > best[key][0]:
                best[key] = (odds, book["name"])
    return [
        Leg(outcome, venue, q_from_decimal(odds), odds, Decimal("1e9"))
        for outcome, (odds, venue) in best.items()
    ]

def polymarket_ml_legs(poly_books: dict[str, dict[str, Any]], event: dict, home: str, away: str) -> list[Leg]:
    legs: list[Leg] = []
    seen: set[tuple[str, str]] = set()
    for market in event.get("markets", []):
        if not market.get("acceptingOrders"):
            continue
        question = str(market.get("question", ""))
        outcomes = parse_json_list(market.get("outcomes"))
        tokens = parse_json_list(market.get("clobTokenIds"))
        if not outcomes or not tokens:
            continue

        lower_outcomes = [str(outcome).strip().lower() for outcome in outcomes]
        if set(lower_outcomes) == {"yes", "no"}:
            outcome = poly_ml_outcome_from_question(question, home, away)
            if not outcome:
                continue
            yes_index = lower_outcomes.index("yes")
            key = (outcome, str(tokens[yes_index]))
            if key in seen:
                continue
            leg = best_poly_leg_from_token(poly_books, str(tokens[yes_index]), outcome)
            if leg:
                seen.add(key)
                legs.append(leg)
            continue

        for outcome_label, token_id in zip(outcomes, tokens):
            label = str(outcome_label)
            if label.lower() == "draw":
                outcome = "Draw"
            else:
                team = canonical_team_from_label(label, home, away)
                outcome = "Home" if team == home else "Away" if team == away else None
            if not outcome:
                continue
            key = (outcome, str(token_id))
            if key in seen:
                continue
            leg = best_poly_leg_from_token(poly_books, str(token_id), outcome)
            if leg:
                seen.add(key)
                legs.append(leg)
    return legs

def maybe_add_mixed_ml_alert(poly_books: dict[str, dict[str, Any]], game: dict, target: dict, extra_alerts: list[dict]) -> None:
    candidates = fiat_ml_legs(game, game["bookies"]) + polymarket_ml_legs(poly_books, target, game["home"], game["away"])
    result = solve_nway(["Home", "Draw", "Away"], candidates, "1000", max_roi=str(SOCCER_MAX_ROI / 100))
    if result.is_arb:
        msg = format_nway_alert(game, "WORLD CUP 3-WAY ML (BEST FIAT/POLY)", result)
        logger.info(msg.replace("\n", " | "))
        extra_alerts.append({"profit": float(result.roi * Decimal("100")), "msg": msg})


def summarize_fiat_markets(bookies: list[dict]) -> str:
    total_lines = sorted({line for book in bookies for line in book.get("totals", {})})
    spread_lines = sorted({line for book in bookies for line in book.get("spreads", {})})
    team_total_lines = sorted({
        line
        for book in bookies
        for lines_by_team in book.get("team_totals", {}).values()
        for line in lines_by_team
    })
    ml_books = sum(1 for book in bookies if book.get("h2h"))
    btts_books = sum(1 for book in bookies if book.get("btts"))
    dc_books = sum(1 for book in bookies if book.get("double_chance"))
    dnb_books = sum(1 for book in bookies if book.get("draw_no_bet"))
    spread_books = sum(1 for book in bookies if book.get("spreads"))
    team_total_books = sum(1 for book in bookies if book.get("team_totals"))
    lines_label = ",".join(f"{line:g}" for line in total_lines[:8]) if total_lines else "none"
    if len(total_lines) > 8:
        lines_label += f",+{len(total_lines) - 8}"
    spreads_label = ",".join(f"{line:g}" for line in spread_lines[:6]) if spread_lines else "none"
    if len(spread_lines) > 6:
        spreads_label += f",+{len(spread_lines) - 6}"
    team_totals_label = ",".join(f"{line:g}" for line in team_total_lines[:6]) if team_total_lines else "none"
    if len(team_total_lines) > 6:
        team_totals_label += f",+{len(team_total_lines) - 6}"
    return (
        f"books={len(bookies)} | ML books={ml_books} | totals lines={lines_label} "
        f"| BTTS books={btts_books} | DC books={dc_books} | DNB books={dnb_books} "
        f"| spread books={spread_books} ({spreads_label}) | team total books={team_total_books} ({team_totals_label})"
    )

def summarize_poly_markets(event: dict, home: str = "", away: str = "") -> str:
    counts = {"win/draw": 0, "totals": 0, "team totals": 0, "btts": 0, "handicap": 0, "other": 0}
    for market in event.get("markets", []):
        if not market.get("acceptingOrders"):
            continue
        question = str(market.get("question", "")).lower()
        if "both teams" in question and "score" in question:
            counts["btts"] += 1
        elif parse_poly_team_total_market(question, home, away)[1] is not None:
            counts["team totals"] += 1
        elif parse_poly_spread_market(question, home, away)[1] is not None:
            counts["handicap"] += 1
        elif "over" in question or "under" in question or "goals" in question:
            counts["totals"] += 1
        elif "win" in question or "draw" in question:
            counts["win/draw"] += 1
        else:
            counts["other"] += 1
    return ", ".join(f"{key}={value}" for key, value in counts.items() if value)

def run_soccer() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try: settings = load_settings()
    except ConfigError as exc: logger.error(f"Config error: {exc}"); return
    clients = ApiClients(settings)
    clients.clear_clob_cache()
    
    try:
        logger.info("📡 Initializing World Cup Soccer Sniper (Pre-Match Hard Kill)...")
        raw_odds, raw_poly = clients.get_soccer_fiat_data(), clients.get_soccer_polymarket_events()
        kalshi = KalshiClient(clients.session, settings.request_timeout_seconds)
        raw_kalshi = []
        for series_ticker in settings.kalshi_series_tickers:
            raw_kalshi.extend(kalshi.get_markets(series_ticker=series_ticker))
        
        fiat_games = {}
        now_utc = datetime.now(timezone.utc)
        cutoff_date = now_utc + timedelta(days=45)
        logger.info(f"   [INFO] World Cup fiat feed returned {len(raw_odds)} events.")
        logger.info(f"   [INFO] Polymarket returned {len(raw_poly)} active events.")
        logger.info(f"   [INFO] Kalshi returned {len(raw_kalshi)} configured soccer markets.")
        scan_stats = {
            "fixtures": 0,
            "poly_matches": 0,
            "poly_missing": 0,
            "fiat_edges": 0,
        }

        for game in raw_odds:
            commence_str = game.get('commence_time')
            commence_utc = datetime.fromisoformat(commence_str.replace("Z", "+00:00"))
            if commence_utc > cutoff_date: continue 
            
            h, a = game.get('home_team'), game.get('away_team')
            if not h or not a: continue
            
            k = f"{clean_for_matching(h)}_{clean_for_matching(a)}"
            if k not in fiat_games: 
                fiat_games[k] = {
                    "home": h, "away": a, "time": commence_str, 
                    "sport_key": game.get('sport_key', 'soccer'), "bookies": []
                }
                
            for b in game.get("bookmakers", []):
                bookmaker_key = str(b.get("key", "")).lower()
                if bookmaker_key not in ALLOWED_FIAT_BOOKMAKER_KEYS:
                    continue

                # Stale Data Firewall (Protects against finished/live games and stale scrapes)
                last_update_str = b.get("last_update")
                if last_update_str:
                    last_update = datetime.fromisoformat(last_update_str.replace("Z", "+00:00"))
                    age_seconds = (now_utc - last_update).total_seconds()
                    
                    is_live = now_utc >= commence_utc
                    
                    # HARD KILL: Drop all live games entirely to prevent Pinnacle Ghost Lines
                    if is_live: 
                        continue
                    
                    # Strict 20-minute (1200s) cutoff for pre-match data 
                    if not is_live and age_seconds > 1200: 
                        continue

                b_data = {
                    "name": b.get("title"),
                    "h2h": {},
                    "totals": {},
                    "btts": {},
                    "double_chance": {},
                    "draw_no_bet": {},
                    "spreads": {},
                    "team_totals": {},
                }
                for m in b.get("markets", []):
                    mk = m.get('key')
                    for o in m.get('outcomes', []):
                        nm, pr = o.get('name'), o.get('price')
                        pt = o.get('point')
                        try:
                            price = Decimal(str(pr)) if pr is not None else None
                        except Exception:
                            price = None
                        if price is None or price <= Decimal("1"):
                            continue

                        if mk in ('h2h', 'h2h_3_way'):
                            if str(nm).lower() == "draw":
                                b_data["h2h"]["Draw"] = price
                            else:
                                team = canonical_team_from_label(str(nm), h, a)
                                if team:
                                    b_data["h2h"][team] = price
                        elif mk in ('totals', 'alternate_totals') and pt is not None:
                            pt_float = float(pt)
                            if pt_float not in b_data["totals"]: b_data["totals"][pt_float] = {}
                            b_data["totals"][pt_float][nm.lower()] = price
                        elif mk == 'btts':
                            b_data["btts"][nm.lower()] = price
                        elif mk == 'double_chance':
                            dc_key = normalize_double_chance_outcome(str(nm), h, a)
                            if dc_key:
                                b_data["double_chance"][dc_key] = price
                        elif mk == 'draw_no_bet':
                            team = canonical_team_from_label(str(nm), h, a)
                            if team:
                                b_data["draw_no_bet"][team] = price
                        elif mk in ('spreads', 'alternate_spreads') and pt is not None:
                            team = canonical_team_from_label(str(nm), h, a)
                            if team:
                                pt_float = float(pt)
                                if pt_float not in b_data["spreads"]:
                                    b_data["spreads"][pt_float] = {}
                                b_data["spreads"][pt_float][team] = price
                        elif mk in ('team_totals', 'alternate_team_totals') and pt is not None:
                            team = canonical_team_from_label(
                                str(o.get("description") or o.get("team") or nm),
                                h,
                                a,
                            )
                            side = str(nm).lower()
                            if team and side in {"over", "under"}:
                                pt_float = float(pt)
                                b_data["team_totals"].setdefault(team, {}).setdefault(pt_float, {})[side] = price
                if (
                    b_data["h2h"]
                    or b_data["totals"]
                    or b_data["btts"]
                    or b_data["double_chance"]
                    or b_data["draw_no_bet"]
                    or b_data["spreads"]
                    or b_data["team_totals"]
                ):
                    fiat_games[k]["bookies"].append(b_data)
        logger.info(f"   [INFO] Built {len(fiat_games)} World Cup fiat games inside 45-day window.")

        opportunities, fiat_opportunities, extra_alerts = [], [], []
        for gk, x in fiat_games.items():
            if not x["bookies"]: continue
            scan_stats["fixtures"] += 1
            h_nk, a_nk = x["home"], x["away"]
            logger.info(f"\n⚽ MATCHED: {x['home']} vs {x['away']} | Local Time: {format_to_local(x['time'])}")
            logger.info("-" * 80)
            logger.info(f"   [SCAN] Fiat markets available: {summarize_fiat_markets(x['bookies'])}")

            fiat_results = fiat_fiat_legs_from_books(x, x["bookies"], bankroll="1000")
            scan_stats["fiat_edges"] += len(fiat_results)
            if not fiat_results:
                logger.info("   [FIAT] No fiat-vs-fiat edge across ML/totals/BTTS/DC/spreads/team totals")
            for label, result in fiat_results:
                msg = format_nway_alert(x, label, result)
                logger.info(msg.replace("\n", " | "))
                extra_alerts.append({"profit": float(result.roi * Decimal("100")), "msg": msg})

            target = None
            for e in raw_poly:
                if (
                    is_team_match(h_nk, e.get('title', ''))
                    and is_team_match(a_nk, e.get('title', ''))
                    and is_same_fixture_time(x["time"], e)
                ):
                    target = e
                    break
                for m in e.get('markets', []):
                    market_text = f"{m.get('question', '')} {m.get('groupItemTitle', '')}"
                    if (
                        is_team_match(h_nk, market_text)
                        and is_team_match(a_nk, market_text)
                        and is_same_fixture_time(x["time"], e)
                    ):
                        target = e
                        break
                if target: break

            poly_books: dict[str, dict[str, Any]] = {}
            if target:
                scan_stats["poly_matches"] += 1
                poly_books = prefetch_polymarket_books(clients, target)
                logger.info(f"   [SCAN] Polymarket match found: {summarize_poly_markets(target, h_nk, a_nk) or 'no accepting markets'}")
                logger.info(f"   [SCAN] Prefetched {len(poly_books)} Polymarket CLOB books.")
                maybe_add_mixed_ml_alert(poly_books, x, target, extra_alerts)
            else:
                scan_stats["poly_missing"] += 1
                logger.info("   [SCAN] Polymarket match not found; Polymarket checks skipped for this fixture")
                        
            for kalshi_market in matching_kalshi_markets(raw_kalshi, h_nk, a_nk):
                ticker = kalshi_market.get("ticker")
                if not ticker:
                    continue
                kalshi_text = " ".join(
                    str(kalshi_market.get(key, ""))
                    for key in ("title", "subtitle", "yes_sub_title", "no_sub_title", "rules_primary")
                ).lower()
                team_in_q = team_from_win_question(kalshi_text, h_nk, a_nk)
                if not team_in_q or team_in_q == "Draw":
                    continue

                kalshi_asks = kalshi.get_orderbook_asks(str(ticker))
                for b in x["bookies"]:
                    f_ml = get_h2h_odds(b, team_in_q)
                    if f_ml:
                        hedge = evaluate_buy_hedge_from_asks(kalshi_asks.get("no", []), f_ml, fee_rate="0.07")
                        kalshi_price = f"${float(hedge.best_ask):.2f}" if hedge.best_ask else "N/A"
                        if hedge.passes_liquidity_filter:
                            roi = round(float((hedge.locked_profit/hedge.total_outlay)*100), 2)
                            logger.info(f"   [KAL-NO]  {b['name']:<10} | Buy Kalshi: NO {team_in_q[:7]} ({kalshi_price:<5}) | Bet Fiat: {team_in_q[:7]} Win ({float(f_ml):<4.2f}) | Status: ✅ ROI {roi}%")
                            if 0 < roi < SOCCER_MAX_ROI:
                                opportunities.append(_build_opp(x, b["name"], f_ml, hedge, "Fiat Win vs Kalshi NO", f"Kalshi NO {team_in_q}", f"{team_in_q} to Win", roi, 0.0, 0.0))
                        else:
                            logger.info(f"   [KAL-NO]  {b['name']:<10} | Buy Kalshi: NO {team_in_q[:7]} ({kalshi_price:<5}) | Bet Fiat: {team_in_q[:7]} Win ({float(f_ml):<4.2f}) | Status: ❌ {hedge.reject_reason}")

                    dc_odds, dc_label = get_double_chance_for_team_not_to_win(b, team_in_q, h_nk, a_nk)
                    if not dc_odds:
                        dc_odds, dc_label = get_synthetic_double_chance_against_team(b, team_in_q, h_nk, a_nk)
                    if dc_odds:
                        hedge = evaluate_buy_hedge_from_asks(kalshi_asks.get("yes", []), dc_odds, fee_rate="0.07")
                        kalshi_price = f"${float(hedge.best_ask):.2f}" if hedge.best_ask else "N/A"
                        if hedge.passes_liquidity_filter:
                            roi = round(float((hedge.locked_profit/hedge.total_outlay)*100), 2)
                            logger.info(f"   [KAL-YES] {b['name']:<10} | Buy Kalshi: YES {team_in_q[:7]} ({kalshi_price:<5}) | Bet Fiat: {dc_label} ({float(dc_odds):<4.2f}) | Status: ✅ ROI {roi}%")
                            if 0 < roi < SOCCER_MAX_ROI:
                                opportunities.append(_build_opp(x, b["name"], dc_odds, hedge, "Double Chance vs Kalshi YES", f"Kalshi YES {team_in_q}", dc_label, roi, 0.0, 0.0))
                        else:
                            logger.info(f"   [KAL-YES] {b['name']:<10} | Buy Kalshi: YES {team_in_q[:7]} ({kalshi_price:<5}) | Bet Fiat: {dc_label} ({float(dc_odds):<4.2f}) | Status: ❌ {hedge.reject_reason}")

            if not target: 
                logger.info(f"   [ML] Polymarket | Status: ❌ No matching market found")
                continue
            
            for b in x["bookies"]:
                for m in target.get('markets', []):
                    if not m.get('acceptingOrders'): continue
                    
                    question = str(m.get('question', '')).lower()
                    outs = parse_json_list(m.get('outcomes'))
                    toks = parse_json_list(m.get('clobTokenIds'))
                    if not outs or not toks:
                        continue
                    
                    if is_draw_market(question):
                        for idx, out_lbl in enumerate(outs):
                            out_lbl, poly_tok = out_lbl.lower(), toks[idx]
                            f_opp, poly_side, fiat_side = None, "", ""
                            if out_lbl == "yes":
                                f_opp, fiat_side = get_no_draw_odds(b, h_nk, a_nk)
                                poly_side = "Draw"
                            elif out_lbl == "no":
                                f_opp = get_draw_odds(b)
                                poly_side, fiat_side = "No Draw", "Draw"
                            if f_opp:
                                book = get_prefetched_poly_book(poly_books, poly_tok)
                                hedge = evaluate_buy_hedge_from_asks(book.get("asks", []), f_opp)
                                poly_price = f"${float(hedge.best_ask):.2f}" if hedge.best_ask else "N/A"

                                if hedge.passes_liquidity_filter:
                                    roi = round(float((hedge.locked_profit/hedge.total_outlay)*100), 2)
                                    logger.info(f"   [DRAW]  {b['name']:<10} | Buy Poly: {poly_side:<10} ({poly_price:<5}) | Bet Fiat: {fiat_side:<10} ({float(f_opp):<4.2f}) | Status: ✅ ROI {roi}%")
                                    if 0 < roi < SOCCER_MAX_ROI: opportunities.append(_build_opp(x, b["name"], f_opp, hedge, "Draw / No Draw", poly_side, fiat_side, roi, 0.0, 0.0))
                                else:
                                    logger.info(f"   [DRAW]  {b['name']:<10} | Buy Poly: {poly_side:<10} ({poly_price:<5}) | Bet Fiat: {fiat_side:<10} ({float(f_opp):<4.2f}) | Status: ❌ {hedge.reject_reason}")

                    elif 'win' in question and not 'over' in question:
                        team_in_q = team_from_win_question(question, h_nk, a_nk)
                        
                        if team_in_q and team_in_q != "Draw":
                            for idx, out_lbl in enumerate(outs):
                                out_lbl = out_lbl.lower()
                                if out_lbl == 'no':
                                    poly_tok = toks[idx]
                                    f_opp = get_h2h_odds(b, team_in_q)
                                    if f_opp:
                                        book = get_prefetched_poly_book(poly_books, poly_tok)
                                        hedge = evaluate_buy_hedge_from_asks(book.get("asks", []), f_opp)
                                        poly_price = f"${float(hedge.best_ask):.2f}" if hedge.best_ask else "N/A"
                                        
                                        if hedge.passes_liquidity_filter:
                                            roi = round(float((hedge.locked_profit/hedge.total_outlay)*100), 2)
                                            logger.info(f"   [DC-NO]  {b['name']:<10} | Buy Poly: NO {team_in_q[:7]} ({poly_price:<5}) | Bet Fiat: {team_in_q[:7]} Win ({float(f_opp):<4.2f}) | Status: ✅ ROI {roi}%")
                                            if 0 < roi < SOCCER_MAX_ROI: opportunities.append(_build_opp(x, b["name"], f_opp, hedge, "Fiat Win vs Poly NO", f"NO {team_in_q}", f"{team_in_q} to Win", roi, 0.0, 0.0))
                                        else:
                                            logger.info(f"   [DC-NO]  {b['name']:<10} | Buy Poly: NO {team_in_q[:7]} ({poly_price:<5}) | Bet Fiat: {team_in_q[:7]} Win ({float(f_opp):<4.2f}) | Status: ❌ {hedge.reject_reason}")
                                            
                                elif out_lbl == 'yes':
                                    poly_tok = toks[idx]
                                    dc_odds, dc_label = get_double_chance_for_team_not_to_win(b, team_in_q, h_nk, a_nk)
                                    if not dc_odds:
                                        dc_odds, dc_label = get_synthetic_double_chance_against_team(b, team_in_q, h_nk, a_nk)
                                    if dc_odds:
                                        book = get_prefetched_poly_book(poly_books, poly_tok)
                                        hedge = evaluate_buy_hedge_from_asks(book.get("asks", []), dc_odds)
                                        poly_price = f"${float(hedge.best_ask):.2f}" if hedge.best_ask else "N/A"
                                        
                                        if hedge.passes_liquidity_filter:
                                            roi = round(float((hedge.locked_profit/hedge.total_outlay)*100), 2)
                                            logger.info(f"   [DC-YES] {b['name']:<10} | Buy Poly: YES {team_in_q[:7]} ({poly_price:<5}) | Bet Fiat: {dc_label} ({float(dc_odds):<4.2f}) | Status: ✅ ROI {roi}%")
                                            if 0 < roi < SOCCER_MAX_ROI: opportunities.append(_build_opp(x, b["name"], dc_odds, hedge, "Double Chance vs Poly YES", f"YES {team_in_q}", dc_label, roi, 0.0, 0.0))
                                        else:
                                            logger.info(f"   [DC-YES] {b['name']:<10} | Buy Poly: YES {team_in_q[:7]} ({poly_price:<5}) | Bet Fiat: {dc_label} ({float(dc_odds):<4.2f}) | Status: ❌ {hedge.reject_reason}")

                    elif 'both teams' in question and 'score' in question:
                        fiat_yes, fiat_no = b["btts"].get('yes'), b["btts"].get('no')
                        for idx, out_lbl in enumerate(outs):
                            out_lbl, poly_tok = out_lbl.lower(), toks[idx]
                            f_opp, poly_side, fiat_side = None, "", ""
                            if out_lbl == 'yes' and fiat_no:
                                f_opp, poly_side, fiat_side = fiat_no, "Yes", "No"
                            elif out_lbl == 'no' and fiat_yes:
                                f_opp, poly_side, fiat_side = fiat_yes, "No", "Yes"
                            if f_opp:
                                book = get_prefetched_poly_book(poly_books, poly_tok)
                                hedge = evaluate_buy_hedge_from_asks(book.get("asks", []), f_opp)
                                poly_price = f"${float(hedge.best_ask):.2f}" if hedge.best_ask else "N/A"

                                if hedge.passes_liquidity_filter:
                                    roi = round(float((hedge.locked_profit/hedge.total_outlay)*100), 2)
                                    logger.info(f"   [BTTS]   {b['name']:<10} | Buy Poly: {poly_side:<10} ({poly_price:<5}) | Bet Fiat: {fiat_side:<10} ({float(f_opp):<4.2f}) | Status: ✅ ROI {roi}%")
                                    if 0 < roi < SOCCER_MAX_ROI: opportunities.append(_build_opp(x, b["name"], f_opp, hedge, "Both Teams to Score", poly_side, fiat_side, roi, 0.0, 0.0))
                                else:
                                    logger.info(f"   [BTTS]   {b['name']:<10} | Buy Poly: {poly_side:<10} ({poly_price:<5}) | Bet Fiat: {fiat_side:<10} ({float(f_opp):<4.2f}) | Status: ❌ {hedge.reject_reason}")

                    elif parse_poly_spread_market(question, h_nk, a_nk)[0]:
                        spread_team, spread_point = parse_poly_spread_market(question, h_nk, a_nk)
                        if spread_team and spread_point is not None:
                            opposite_team = a_nk if spread_team == h_nk else h_nk
                            for idx, out_lbl in enumerate(outs):
                                out_lbl, poly_tok = out_lbl.lower(), toks[idx]
                                f_opp, poly_side, fiat_side = None, "", ""
                                if out_lbl == "yes":
                                    f_opp = get_spread_odds(b, opposite_team, -spread_point)
                                    poly_side, fiat_side = f"{spread_team} {spread_point:+g}", f"{opposite_team} {-spread_point:+g}"
                                elif out_lbl == "no":
                                    f_opp = get_spread_odds(b, spread_team, spread_point)
                                    poly_side, fiat_side = f"{opposite_team} {-spread_point:+g}", f"{spread_team} {spread_point:+g}"
                                if f_opp:
                                    book = get_prefetched_poly_book(poly_books, poly_tok)
                                    hedge = evaluate_buy_hedge_from_asks(book.get("asks", []), f_opp)
                                    poly_price = f"${float(hedge.best_ask):.2f}" if hedge.best_ask else "N/A"

                                    if hedge.passes_liquidity_filter:
                                        roi = round(float((hedge.locked_profit/hedge.total_outlay)*100), 2)
                                        logger.info(f"   [SPRD]   {b['name']:<10} | Buy Poly: {poly_side[:16]:<16} ({poly_price:<5}) | Bet Fiat: {fiat_side[:16]:<16} ({float(f_opp):<4.2f}) | Status: ✅ ROI {roi}%")
                                        if 0 < roi < SOCCER_MAX_ROI: opportunities.append(_build_opp(x, b["name"], f_opp, hedge, "World Cup Handicap / Spread", poly_side, fiat_side, roi, 0.0, 0.0))
                                    else:
                                        logger.info(f"   [SPRD]   {b['name']:<10} | Buy Poly: {poly_side[:16]:<16} ({poly_price:<5}) | Bet Fiat: {fiat_side[:16]:<16} ({float(f_opp):<4.2f}) | Status: ❌ {hedge.reject_reason}")

                    elif 'over' in question or 'under' in question or 'goals' in question:
                        team_total_team, team_total_line, team_total_yes_side = parse_poly_team_total_market(question, h_nk, a_nk)
                        if team_total_team and team_total_line is not None:
                            for idx, out_lbl in enumerate(outs):
                                out_lbl, poly_tok = out_lbl.lower(), toks[idx]
                                poly_raw_side = poly_side_from_outcome(out_lbl, team_total_yes_side)
                                f_opp, poly_side, fiat_side = None, "", ""
                                if poly_raw_side == "over":
                                    f_opp = get_team_total_odds(b, team_total_team, team_total_line, "under")
                                    poly_side, fiat_side = f"{team_total_team} Over {team_total_line}", f"{team_total_team} Under {team_total_line}"
                                elif poly_raw_side == "under":
                                    f_opp = get_team_total_odds(b, team_total_team, team_total_line, "over")
                                    poly_side, fiat_side = f"{team_total_team} Under {team_total_line}", f"{team_total_team} Over {team_total_line}"
                                if f_opp:
                                    book = get_prefetched_poly_book(poly_books, poly_tok)
                                    hedge = evaluate_buy_hedge_from_asks(book.get("asks", []), f_opp)
                                    poly_price = f"${float(hedge.best_ask):.2f}" if hedge.best_ask else "N/A"

                                    if hedge.passes_liquidity_filter:
                                        roi = round(float((hedge.locked_profit/hedge.total_outlay)*100), 2)
                                        logger.info(f"   [TTOT]   {b['name']:<10} | Buy Poly: {poly_side[:16]:<16} ({poly_price:<5}) | Bet Fiat: {fiat_side[:16]:<16} ({float(f_opp):<4.2f}) | Status: ✅ ROI {roi}%")
                                        if 0 < roi < SOCCER_MAX_ROI: opportunities.append(_build_opp(x, b["name"], f_opp, hedge, f"Team Total Goals {team_total_line}", poly_side, fiat_side, roi, 0.0, 0.0))
                                    else:
                                        logger.info(f"   [TTOT]   {b['name']:<10} | Buy Poly: {poly_side[:16]:<16} ({poly_price:<5}) | Bet Fiat: {fiat_side[:16]:<16} ({float(f_opp):<4.2f}) | Status: ❌ {hedge.reject_reason}")
                            continue

                        spread_team, spread_point = parse_poly_spread_market(question, h_nk, a_nk)
                        if spread_team and spread_point is not None:
                            opposite_team = a_nk if spread_team == h_nk else h_nk
                            for idx, out_lbl in enumerate(outs):
                                out_lbl, poly_tok = out_lbl.lower(), toks[idx]
                                f_opp, poly_side, fiat_side = None, "", ""
                                if out_lbl == "yes":
                                    f_opp = get_spread_odds(b, opposite_team, -spread_point)
                                    poly_side, fiat_side = f"{spread_team} {spread_point:+g}", f"{opposite_team} {-spread_point:+g}"
                                elif out_lbl == "no":
                                    f_opp = get_spread_odds(b, spread_team, spread_point)
                                    poly_side, fiat_side = f"{opposite_team} {-spread_point:+g}", f"{spread_team} {spread_point:+g}"
                                if f_opp:
                                    book = get_prefetched_poly_book(poly_books, poly_tok)
                                    hedge = evaluate_buy_hedge_from_asks(book.get("asks", []), f_opp)
                                    poly_price = f"${float(hedge.best_ask):.2f}" if hedge.best_ask else "N/A"

                                    if hedge.passes_liquidity_filter:
                                        roi = round(float((hedge.locked_profit/hedge.total_outlay)*100), 2)
                                        logger.info(f"   [SPRD]   {b['name']:<10} | Buy Poly: {poly_side[:16]:<16} ({poly_price:<5}) | Bet Fiat: {fiat_side[:16]:<16} ({float(f_opp):<4.2f}) | Status: ✅ ROI {roi}%")
                                        if 0 < roi < SOCCER_MAX_ROI: opportunities.append(_build_opp(x, b["name"], f_opp, hedge, "World Cup Handicap / Spread", poly_side, fiat_side, roi, 0.0, 0.0))
                                    else:
                                        logger.info(f"   [SPRD]   {b['name']:<10} | Buy Poly: {poly_side[:16]:<16} ({poly_price:<5}) | Bet Fiat: {fiat_side[:16]:<16} ({float(f_opp):<4.2f}) | Status: ❌ {hedge.reject_reason}")
                            continue

                        line, yes_side = parse_poly_total_market(question)
                        if line is None:
                            continue
                        if line not in b.get("totals", {}): continue
                        fiat_over, fiat_under = b["totals"][line].get('over'), b["totals"][line].get('under')
                        for idx, out_lbl in enumerate(outs):
                            out_lbl, poly_tok = out_lbl.lower(), toks[idx]
                            poly_raw_side = poly_side_from_outcome(out_lbl, yes_side)
                            f_opp, poly_side, fiat_side = None, "", ""
                            if poly_raw_side == 'over' and fiat_under:
                                f_opp, poly_side, fiat_side = fiat_under, f"Over {line}", f"Under {line}"
                            elif poly_raw_side == 'under' and fiat_over:
                                f_opp, poly_side, fiat_side = fiat_over, f"Under {line}", f"Over {line}"
                            if f_opp:
                                book = get_prefetched_poly_book(poly_books, poly_tok)
                                hedge = evaluate_buy_hedge_from_asks(book.get("asks", []), f_opp)
                                poly_price = f"${float(hedge.best_ask):.2f}" if hedge.best_ask else "N/A"
                                
                                if hedge.passes_liquidity_filter:
                                    roi = round(float((hedge.locked_profit/hedge.total_outlay)*100), 2)
                                    logger.info(f"   [TOT]    {b['name']:<10} | Buy Poly: {poly_side[:10]:<10} ({poly_price:<5}) | Bet Fiat: {fiat_side[:10]:<10} ({float(f_opp):<4.2f}) | Status: ✅ ROI {roi}%")
                                    if 0 < roi < SOCCER_MAX_ROI: opportunities.append(_build_opp(x, b["name"], f_opp, hedge, f"Total Goals {line}", poly_side, fiat_side, roi, 0.0, 0.0))
                                else:
                                    logger.info(f"   [TOT]    {b['name']:<10} | Buy Poly: {poly_side[:10]:<10} ({poly_price:<5}) | Bet Fiat: {fiat_side[:10]:<10} ({float(f_opp):<4.2f}) | Status: ❌ {hedge.reject_reason}")

        logger.info("\n" + "="*80)
        final_alerts = build_soccer_global_alerts(opportunities, fiat_opportunities, limit=3, extra_alerts=extra_alerts)
        for msg in final_alerts: clients.send_telegram_alert(msg)
        logger.info(
            f"📊 WORLD CUP SOCCER SCAN SUMMARY: fixtures={scan_stats['fixtures']} | "
            f"poly matched={scan_stats['poly_matches']} | poly missing={scan_stats['poly_missing']} | "
            f"fiat edges={scan_stats['fiat_edges']} | web3 edges={len(opportunities)}"
        )
        logger.info(f"✅ WORLD CUP SOCCER SCAN COMPLETE. Sent {len(final_alerts)} alerts.")
        logger.info("="*80)
    finally: clients.close()

def _build_opp(x, b, f_o, hedge, m, p_s, f_s, roi, dt, sp):
    return ArbitrageOpportunity("soccer", x['home'], x['away'], format_to_local(x['time']), m, p_s, f_s, b, float(f_o), float(hedge.shares), float(hedge.vwap or 0), float(hedge.marginal_price or 0), float(hedge.poly_spend), float(hedge.poly_fees), float(hedge.sportsbook_stake), float(hedge.total_outlay), float(hedge.locked_profit), roi, dt, sp)
