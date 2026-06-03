import logging
import json
import unicodedata
import re
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional
from datetime import datetime, timedelta, timezone
from decimal import Decimal, getcontext
from zoneinfo import ZoneInfo
from thefuzz import fuzz

from .api_clients import ApiClients
from .config import ConfigError, load_settings
from .models import ArbitrageOpportunity, FiatArbitrageOpportunity
from .alerts import build_soccer_global_alerts
from .arb_core import fiat_fiat_legs_from_books, format_nway_alert
from .kalshi_client import KalshiClient

logger = logging.getLogger(__name__)
getcontext().prec = 28
SOCCER_MAX_ROI = 15.0

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

def run_soccer() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try: settings = load_settings()
    except ConfigError as exc: logger.error(f"Config error: {exc}"); return
    clients = ApiClients(settings)
    clients.clear_clob_cache()
    
    try:
        logger.info("📡 Initializing Global Soccer Sniper (Pre-Match Hard Kill)...")
        raw_odds, raw_poly = clients.get_soccer_fiat_data(), clients.get_soccer_polymarket_events()
        kalshi = KalshiClient(clients.session, settings.request_timeout_seconds)
        raw_kalshi = []
        for series_ticker in settings.kalshi_series_tickers:
            raw_kalshi.extend(kalshi.get_markets(series_ticker=series_ticker))
        
        fiat_games = {}
        now_utc = datetime.now(timezone.utc)
        cutoff_date = now_utc + timedelta(days=45)
        logger.info(f"   [INFO] Soccer fiat feed returned {len(raw_odds)} events.")
        logger.info(f"   [INFO] Polymarket returned {len(raw_poly)} active events.")
        logger.info(f"   [INFO] Kalshi returned {len(raw_kalshi)} configured soccer markets.")

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

                b_data = {"name": b.get("title"), "h2h": {}, "totals": {}, "btts": {}, "double_chance": {}}
                for m in b.get("markets", []):
                    mk = m.get('key')
                    for o in m.get('outcomes', []):
                        nm, pr = o.get('name'), o.get('price')
                        pt = o.get('point')
                        if mk == 'h2h' and pr is not None:
                            b_data["h2h"][nm] = Decimal(str(pr))
                        elif mk in ('totals', 'alternate_totals') and pr is not None and pt is not None:
                            pt_float = float(pt)
                            if pt_float not in b_data["totals"]: b_data["totals"][pt_float] = {}
                            b_data["totals"][pt_float][nm.lower()] = Decimal(str(pr))
                        elif mk == 'btts' and pr is not None:
                            b_data["btts"][nm.lower()] = Decimal(str(pr))
                        elif mk == 'double_chance' and pr is not None:
                            dc_key = normalize_double_chance_outcome(str(nm), h, a)
                            if dc_key:
                                b_data["double_chance"][dc_key] = Decimal(str(pr))
                if b_data["h2h"] or b_data["totals"] or b_data["btts"] or b_data["double_chance"]:
                    fiat_games[k]["bookies"].append(b_data)
        logger.info(f"   [INFO] Built {len(fiat_games)} fiat soccer games inside 45-day window.")

        opportunities, fiat_opportunities, extra_alerts = [], [], []
        for gk, x in fiat_games.items():
            if not x["bookies"]: continue
            h_nk, a_nk = x["home"], x["away"]
            logger.info(f"\n⚽ MATCHED: {x['home']} vs {x['away']} | Local Time: {format_to_local(x['time'])}")
            logger.info("-" * 80)

            for label, result in fiat_fiat_legs_from_books(x, x["bookies"], bankroll="1000"):
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
                    try:
                        outs, toks = json.loads(m.get('outcomes')), json.loads(m.get('clobTokenIds'))
                    except: continue
                    
                    if 'win' in question and not 'over' in question:
                        team_in_q = team_from_win_question(question, h_nk, a_nk)
                        
                        if team_in_q and team_in_q != "Draw":
                            for idx, out_lbl in enumerate(outs):
                                out_lbl = out_lbl.lower()
                                if out_lbl == 'no':
                                    poly_tok = toks[idx]
                                    f_opp = get_h2h_odds(b, team_in_q)
                                    if f_opp:
                                        book = clients.get_clob_book(poly_tok)
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
                                        book = clients.get_clob_book(poly_tok)
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
                                book = clients.get_clob_book(poly_tok)
                                hedge = evaluate_buy_hedge_from_asks(book.get("asks", []), f_opp)
                                poly_price = f"${float(hedge.best_ask):.2f}" if hedge.best_ask else "N/A"

                                if hedge.passes_liquidity_filter:
                                    roi = round(float((hedge.locked_profit/hedge.total_outlay)*100), 2)
                                    logger.info(f"   [BTTS]   {b['name']:<10} | Buy Poly: {poly_side:<10} ({poly_price:<5}) | Bet Fiat: {fiat_side:<10} ({float(f_opp):<4.2f}) | Status: ✅ ROI {roi}%")
                                    if 0 < roi < SOCCER_MAX_ROI: opportunities.append(_build_opp(x, b["name"], f_opp, hedge, "Both Teams to Score", poly_side, fiat_side, roi, 0.0, 0.0))
                                else:
                                    logger.info(f"   [BTTS]   {b['name']:<10} | Buy Poly: {poly_side:<10} ({poly_price:<5}) | Bet Fiat: {fiat_side:<10} ({float(f_opp):<4.2f}) | Status: ❌ {hedge.reject_reason}")

                    elif 'over' in question or 'under' in question or 'goals' in question:
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
                                book = clients.get_clob_book(poly_tok)
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
        logger.info(f"✅ SOCCER SCAN COMPLETE. Sent {len(final_alerts)} alerts.")
        logger.info("="*80)
    finally: clients.close()

def _build_opp(x, b, f_o, hedge, m, p_s, f_s, roi, dt, sp):
    return ArbitrageOpportunity("soccer", x['home'], x['away'], format_to_local(x['time']), m, p_s, f_s, b, float(f_o), float(hedge.shares), float(hedge.vwap or 0), float(hedge.marginal_price or 0), float(hedge.poly_spend), float(hedge.poly_fees), float(hedge.sportsbook_stake), float(hedge.total_outlay), float(hedge.locked_profit), roi, dt, sp)
