import logging
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional
from datetime import datetime, timedelta, timezone
from decimal import Decimal, getcontext
from zoneinfo import ZoneInfo
from thefuzz import fuzz

from .api_clients import ApiClients
from .config import ConfigError, load_settings
from .models import ArbitrageOpportunity, FiatArbitrageOpportunity
from .alerts import build_baseball_global_alerts

logger = logging.getLogger(__name__)
getcontext().prec = 28
BASEBALL_MAX_ROI = 15.0
BASEBALL_STALE_SECONDS = 300
BASEBALL_START_BUFFER_MINUTES = 10
BASEBALL_POLY_TIME_TOLERANCE_SECONDS = 1800
BASEBALL_POLY_MARKET_TYPES = {"moneyline", "totals", "spreads"}

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
            if s > 0:
                levels.append(BookLevel(price=p, size=s))
        except Exception:
            pass
    return sorted(levels, key=lambda lvl: lvl.price)

def fee_per_share(p: Decimal, r: Decimal) -> Decimal:
    return r * p * (Decimal("1") - p)

def evaluate_buy_hedge_from_asks(asks, decimal_odds, bankroll="100", fee_rate="0.03", max_avg_impact_rel="0.02"):
    levels = normalize_asks(asks)
    odds, bankroll_d, fee_r = Decimal(str(decimal_odds)), Decimal(bankroll), Decimal(fee_rate)
    inv_odds = Decimal("1") / odds
    eps = Decimal("0.0000000001")

    if not levels:
        return HedgeEstimate(None, Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), None, None, Decimal("0"), False, "Empty Orderbook")

    best = levels[0]
    q, cost, fees = Decimal("0"), Decimal("0"), Decimal("0")
    marginal, full_bankroll_supported = None, False

    for lvl in levels:
        lvl_fee_ps = fee_per_share(lvl.price, fee_r)
        lvl_all_in_ps = lvl.price + lvl_fee_ps + inv_odds
        if lvl_all_in_ps >= Decimal("1"):
            break
        rem = bankroll_d - ((q * inv_odds) + cost + fees)
        if rem <= eps:
            full_bankroll_supported = True
            break
        affordable = rem / lvl_all_in_ps
        take = min(lvl.size, affordable)
        if take <= 0:
            break
        q += take
        cost += take * lvl.price
        fees += take * lvl_fee_ps
        marginal = lvl.price
        if take < lvl.size:
            full_bankroll_supported = True
            break

    total = cost + fees + (q * inv_odds)
    if total >= bankroll_d - eps:
        full_bankroll_supported = True
    if q <= Decimal("0"):
        return HedgeEstimate(best.price, Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), None, None, Decimal("0"), False, "No profitable depth")

    vwap = cost / q
    profit = q - total
    impact = (vwap / best.price) - Decimal("1")
    reason = None
    if not full_bankroll_supported:
        reason = "Insufficient depth for $100 bankroll"
    elif impact > Decimal(max_avg_impact_rel):
        reason = "Slippage exceeds 2% buffer"
    elif profit <= 0:
        reason = "Negative profit after fees"

    return HedgeEstimate(best.price, q, (q / odds), cost, fees, total, vwap, marginal, profit, (reason is None), reason)

def clean_for_matching(text: str) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", str(text)).encode("ASCII", "ignore").decode("utf-8").lower()
    text = text.replace("&", " and ").replace("/", " ").replace("-", " ").replace(".", " ")
    text = re.sub(r"\bthe\b", " ", text)
    return re.sub(r"[^a-z0-9\s]", "", text)

def match_score(left: str, right: str) -> int:
    return fuzz.token_set_ratio(clean_for_matching(left), clean_for_matching(right))

def is_team_match(team: str, text: str) -> bool:
    team_clean = clean_for_matching(team)
    text_clean = clean_for_matching(text)
    if not team_clean or not text_clean:
        return False
    if fuzz.token_set_ratio(team_clean, text_clean) >= 82:
        return True

    team_tokens = [token for token in team_clean.split() if len(token) > 1]
    text_tokens = set(text_clean.split())
    if team_tokens and all(token in text_tokens for token in team_tokens):
        return True

    nickname = team_tokens[-1] if team_tokens else ""
    return len(nickname) > 3 and nickname in text_tokens

def has_matchup_marker(text: str) -> bool:
    text = str(text).lower().replace("vs.", "vs").replace("@", " @ ")
    return " vs " in text or " v " in text or " @ " in text

def is_matchup_match(home: str, away: str, poly_text: str) -> bool:
    return has_matchup_marker(poly_text) and is_team_match(home, poly_text) and is_team_match(away, poly_text)

def resolve_outcome_team(outcome: str, home: str, away: str) -> Optional[str]:
    home_score, away_score = match_score(outcome, home), match_score(outcome, away)
    if home_score >= 82 and home_score > away_score:
        return home
    if away_score >= 82 and away_score > home_score:
        return away
    if is_team_match(outcome, home) and not is_team_match(outcome, away):
        return home
    if is_team_match(outcome, away) and not is_team_match(outcome, home):
        return away
    return None

def opposing_team(team: str, home: str, away: str) -> str:
    return away if team == home else home

def get_h2h_odds(bookie: dict, selection: str) -> Optional[Decimal]:
    for name, odds in bookie.get("h2h", {}).items():
        if is_team_match(selection, name) or is_team_match(name, selection):
            return odds
    return None

def get_spread_odds(bookie: dict, selection: str, point: float) -> Optional[Decimal]:
    odds_by_team = bookie.get("spreads", {}).get(round(float(point), 1), {})
    for name, odds in odds_by_team.items():
        if is_team_match(selection, name) or is_team_match(name, selection):
            return odds
    return None

def parse_utc_datetime(value) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)

def get_poly_market_start(event: dict, market: dict) -> Optional[datetime]:
    for source in (market, event):
        for key in ("gameStartTime", "startTime"):
            parsed = parse_utc_datetime(source.get(key))
            if parsed:
                return parsed
    return None

def is_poly_time_match(commence_utc: datetime, event: dict, market: dict) -> bool:
    poly_start = get_poly_market_start(event, market)
    if not poly_start:
        return False
    delta = abs((poly_start - commence_utc).total_seconds())
    return delta <= BASEBALL_POLY_TIME_TOLERANCE_SECONDS

def is_poly_market_actionable(event: dict, market: dict, now_utc: datetime) -> bool:
    if event.get("live") is True:
        return False
    if event.get("closed") or market.get("closed"):
        return False
    if event.get("active") is False or market.get("active") is False:
        return False
    if not market.get("acceptingOrders") or not market.get("enableOrderBook", True):
        return False

    poly_start = get_poly_market_start(event, market)
    if not poly_start:
        return False
    return poly_start > now_utc + timedelta(minutes=BASEBALL_START_BUFFER_MINUTES)

def get_spread_odds_for_team(bookie: dict, selection: str, point: float, home: str, away: str) -> Optional[tuple[str, Decimal]]:
    odds_by_team = bookie.get("spreads", {}).get(round(float(point), 1), {})
    expected_team = resolve_outcome_team(selection, home, away)
    if not expected_team:
        return None

    for name, odds in odds_by_team.items():
        resolved_name = resolve_outcome_team(name, home, away)
        if resolved_name == expected_team:
            return resolved_name, odds
    return None

def is_same_bookmaker(b1: dict, b2: dict) -> bool:
    left = str(b1.get("key") or b1.get("name") or "").strip().lower()
    right = str(b2.get("key") or b2.get("name") or "").strip().lower()
    return bool(left and right and left == right)

def load_json_list(value) -> list:
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        loaded = json.loads(value)
        return loaded if isinstance(loaded, list) else []
    except Exception:
        return []

def format_to_local(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ZoneInfo("America/Toronto")).strftime("%Y-%m-%d %I:%M %p")
    except Exception:
        return iso[:10]

def format_point(point: float) -> str:
    return f"{point:+.1f}"

def parse_total_line(text: str) -> Optional[float]:
    line_match = re.search(r"(?:o/u|over/under)\s*(\d+(?:\.\d+)?)", text.lower())
    if not line_match:
        return None
    return round(float(line_match.group(1)), 1)

def parse_poly_spread(market: dict, home: str, away: str) -> Optional[tuple[str, float]]:
    question = str(market.get("question", ""))
    match = re.search(r"Spread:\s*(.+?)\s*\(([+-]?\d+(?:\.\d+)?)\)", question, re.IGNORECASE)
    if not match:
        return None

    anchor = resolve_outcome_team(match.group(1), home, away)
    if not anchor:
        return None

    return anchor, round(float(match.group(2)), 1)

def run_baseball() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        settings = load_settings()
    except ConfigError as exc:
        logger.error(f"Config error: {exc}")
        return
    clients = ApiClients(settings)

    try:
        logger.info("📡 Initializing Baseball Sniper...")
        raw_odds, raw_poly = clients.get_baseball_fiat_data(), clients.get_baseball_polymarket_events()
        logger.info(f"   [INFO] Baseball Odds API returned {len(raw_odds)} events.")
        logger.info(f"   [INFO] Polymarket returned {len(raw_poly)} baseball events.")

        fiat_games = {}
        now_utc = datetime.now(timezone.utc)
        cutoff_date = now_utc + timedelta(days=7)
        actionable_start_cutoff = now_utc + timedelta(minutes=BASEBALL_START_BUFFER_MINUTES)

        for game in raw_odds:
            commence_str = game.get("commence_time")
            if not commence_str:
                continue
            commence_utc = datetime.fromisoformat(commence_str.replace("Z", "+00:00"))
            if commence_utc <= actionable_start_cutoff or commence_utc > cutoff_date:
                if commence_utc <= now_utc:
                    logger.info(f"   [SKIP] Live/started game removed: {game.get('home_team')} vs {game.get('away_team')} | {format_to_local(commence_str)}")
                elif commence_utc <= actionable_start_cutoff:
                    logger.info(f"   [SKIP] Near-start game removed: {game.get('home_team')} vs {game.get('away_team')} | {format_to_local(commence_str)}")
                continue

            h, a = game.get("home_team"), game.get("away_team")
            if not h or not a:
                continue

            key_parts = sorted([clean_for_matching(h), clean_for_matching(a)])
            k = "_".join(key_parts)
            if k not in fiat_games:
                fiat_games[k] = {
                    "home": h, "away": a, "time": commence_str,
                    "sport_key": game.get("sport_key", "baseball"), "bookies": []
                }

            for b in game.get("bookmakers", []):
                if commence_utc <= actionable_start_cutoff:
                    continue
                last_update_str = b.get("last_update")
                if last_update_str:
                    last_update = datetime.fromisoformat(last_update_str.replace("Z", "+00:00"))
                    age_seconds = (now_utc - last_update).total_seconds()
                    if age_seconds > BASEBALL_STALE_SECONDS:
                        continue

                b_data = {"key": b.get("key"), "name": b.get("title"), "h2h": {}, "totals": {}, "spreads": {}}
                for m in b.get("markets", []):
                    mk = m.get("key")
                    for o in m.get("outcomes", []):
                        nm, pr = o.get("name"), o.get("price")
                        pt = o.get("point")
                        if pr is None:
                            continue
                        if mk == "h2h":
                            b_data["h2h"][str(nm)] = Decimal(str(pr))
                        elif mk == "totals" and pt is not None:
                            pt_float = round(float(pt), 1)
                            if pt_float not in b_data["totals"]:
                                b_data["totals"][pt_float] = {}
                            b_data["totals"][pt_float][str(nm).lower()] = Decimal(str(pr))
                        elif mk == "spreads" and pt is not None:
                            pt_float = round(float(pt), 1)
                            if pt_float not in b_data["spreads"]:
                                b_data["spreads"][pt_float] = {}
                            b_data["spreads"][pt_float][str(nm)] = Decimal(str(pr))
                if b_data["h2h"] or b_data["totals"] or b_data["spreads"]:
                    fiat_games[k]["bookies"].append(b_data)

        opportunities, fiat_opportunities = [], []
        for gk, x in fiat_games.items():
            if not x["bookies"]:
                continue
            h, a = x["home"], x["away"]
            logger.info(f"\n⚾ MATCHED: {h} vs {a} | Local Time: {format_to_local(x['time'])}")
            logger.info("-" * 80)

            # 1. Fiat Scanner (Moneyline, totals, and run line)
            for i in range(len(x["bookies"])):
                for j in range(i + 1, len(x["bookies"])):
                    b1, b2 = x["bookies"][i], x["bookies"][j]
                    if is_same_bookmaker(b1, b2):
                        continue

                    for sel, o1 in b1["h2h"].items():
                        matched = resolve_outcome_team(sel, h, a)
                        if not matched:
                            continue
                        opp_team = opposing_team(matched, h, a)
                        o2 = get_h2h_odds(b2, opp_team)
                        if o1 and o2:
                            imp = (Decimal("1") / o1) + (Decimal("1") / o2)
                            if imp < 1:
                                roi = round(((1 / float(imp)) - 1) * 100, 2)
                                if 0 < roi < BASEBALL_MAX_ROI:
                                    fiat_opportunities.append(_build_fiat_opp(x, b1["name"], b2["name"], o1, o2, "Moneyline", matched, opp_team, imp, roi))

                    for pt, t1_odds in b1.get("totals", {}).items():
                        t2_odds = b2.get("totals", {}).get(pt, {})
                        o1_over, o1_under = t1_odds.get("over"), t1_odds.get("under")
                        o2_over, o2_under = t2_odds.get("over"), t2_odds.get("under")

                        if o1_over and o2_under:
                            imp = (Decimal("1") / o1_over) + (Decimal("1") / o2_under)
                            if imp < 1:
                                roi = round(((1 / float(imp)) - 1) * 100, 2)
                                if 0 < roi < BASEBALL_MAX_ROI:
                                    fiat_opportunities.append(_build_fiat_opp(x, b1["name"], b2["name"], o1_over, o2_under, f"Total Runs {pt}", f"Over {pt}", f"Under {pt}", imp, roi))
                        if o1_under and o2_over:
                            imp = (Decimal("1") / o1_under) + (Decimal("1") / o2_over)
                            if imp < 1:
                                roi = round(((1 / float(imp)) - 1) * 100, 2)
                                if 0 < roi < BASEBALL_MAX_ROI:
                                    fiat_opportunities.append(_build_fiat_opp(x, b1["name"], b2["name"], o1_under, o2_over, f"Total Runs {pt}", f"Under {pt}", f"Over {pt}", imp, roi))

                    for pt, spread_odds in b1.get("spreads", {}).items():
                        for sel, o1 in spread_odds.items():
                            matched = resolve_outcome_team(sel, h, a)
                            if not matched:
                                continue
                            opp_team = opposing_team(matched, h, a)
                            resolved = get_spread_odds_for_team(b2, opp_team, -pt, h, a)
                            if not resolved:
                                continue
                            resolved_team, o2 = resolved
                            if resolved_team != opp_team:
                                continue
                            if matched == resolved_team:
                                logger.info(f"   [RL] Validator | Status: ❌ Rejected same-team run-line pair {matched} {format_point(pt)} / {resolved_team} {format_point(-pt)}")
                                continue
                            if o1 and o2:
                                imp = (Decimal("1") / o1) + (Decimal("1") / o2)
                                if imp < 1:
                                    roi = round(((1 / float(imp)) - 1) * 100, 2)
                                    if 0 < roi < BASEBALL_MAX_ROI:
                                        fiat_opportunities.append(_build_fiat_opp(x, b1["name"], b2["name"], o1, o2, f"Run Line {format_point(pt)}", f"{matched} {format_point(pt)}", f"{opp_team} {format_point(-pt)}", imp, roi))

            # 2. Poly Scanner (Moneyline, totals, and run line)
            target_markets = []
            team_matched_poly_markets = 0
            seen_poly_market_ids: set[str] = set()
            fiat_commence_utc = parse_utc_datetime(x["time"])
            if not fiat_commence_utc:
                logger.info("   [ML] Polymarket | Status: ❌ Missing fiat start time")
                continue

            for e in raw_poly:
                event_title = e.get("title", "")
                event_title_matches = is_matchup_match(h, a, event_title)

                for m in e.get("markets", []):
                    market_text = f"{m.get('question', '')} {m.get('groupItemTitle', '')}"
                    if not event_title_matches and not is_matchup_match(h, a, market_text):
                        continue

                    team_matched_poly_markets += 1
                    market_type = str(m.get("sportsMarketType", "")).lower()
                    if market_type not in BASEBALL_POLY_MARKET_TYPES:
                        continue
                    if not is_poly_market_actionable(e, m, now_utc):
                        continue
                    if not is_poly_time_match(fiat_commence_utc, e, m):
                        continue

                    market_id = str(m.get("id") or m.get("conditionId") or m.get("questionID") or "")
                    if market_id and market_id in seen_poly_market_ids:
                        continue
                    if market_id:
                        seen_poly_market_ids.add(market_id)

                    target_markets.append((e, m))

            if not target_markets:
                if team_matched_poly_markets:
                    logger.info("   [ML] Polymarket | Status: ❌ Same matchup found, but no time-matched actionable market")
                else:
                    logger.info("   [ML] Polymarket | Status: ❌ No matching market found")
                continue

            logger.info(f"   [INFO] Polymarket time-matched markets: {len(target_markets)}")

            for b in x["bookies"]:
                for target, m in target_markets:
                    if not m.get("acceptingOrders"):
                        continue
                    mt = str(m.get("sportsMarketType", "")).lower()
                    question = str(m.get("question", "")).lower()
                    group_title = str(m.get("groupItemTitle", "")).lower()
                    market_context = f"{question} {group_title}"

                    outs, toks = load_json_list(m.get("outcomes")), load_json_list(m.get("clobTokenIds"))
                    if not outs or not toks or len(outs) != len(toks):
                        continue

                    if mt == "moneyline":
                        for idx, outcome in enumerate(outs):
                            if idx >= len(toks):
                                continue
                            matched = resolve_outcome_team(str(outcome), h, a)
                            if not matched:
                                continue
                            opp_team = opposing_team(matched, h, a)
                            f_opp = get_h2h_odds(b, opp_team)
                            if f_opp:
                                book = clients.get_clob_book(toks[idx])
                                hedge = evaluate_buy_hedge_from_asks(book.get("asks", []), f_opp)
                                poly_price = f"${float(hedge.best_ask):.2f}" if hedge.best_ask else "N/A"
                                logger.info(f"   [ML] {b['name']:<10} | {str(outcome)[:12]:<12} | {b['name']} Opp: {float(f_opp):<5} | Poly Ask: {poly_price:<5} | Status: {'✅' if hedge.passes_liquidity_filter else '❌ ' + str(hedge.reject_reason)}")
                                if hedge.passes_liquidity_filter:
                                    roi = round(float((hedge.locked_profit / hedge.total_outlay) * 100), 2)
                                    if 0 < roi < BASEBALL_MAX_ROI:
                                        opportunities.append(_build_opp(x, b["name"], f_opp, hedge, "Moneyline", str(outcome), opp_team, roi, 0.0, 0.0))

                    elif mt == "totals":
                        line = parse_total_line(market_context)
                        if line is None or line not in b.get("totals", {}):
                            continue

                        fiat_over = b["totals"][line].get("over")
                        fiat_under = b["totals"][line].get("under")

                        for idx, out_lbl in enumerate(outs):
                            if idx >= len(toks):
                                continue
                            out_clean = str(out_lbl).lower()
                            f_opp, poly_side, fiat_side = None, "", ""
                            if out_clean.startswith("over"):
                                f_opp = fiat_under
                                poly_side = f"Over {line}"
                                fiat_side = f"Under {line}"
                            elif out_clean.startswith("under"):
                                f_opp = fiat_over
                                poly_side = f"Under {line}"
                                fiat_side = f"Over {line}"

                            if f_opp:
                                book = clients.get_clob_book(toks[idx])
                                hedge = evaluate_buy_hedge_from_asks(book.get("asks", []), f_opp)
                                poly_price = f"${float(hedge.best_ask):.2f}" if hedge.best_ask else "N/A"
                                logger.info(f"   [TOT] {b['name']:<9} | {poly_side[:10]:<10} | {b['name']} Opp: {float(f_opp):<5} | Poly Ask: {poly_price:<5} | Status: {'✅' if hedge.passes_liquidity_filter else '❌ ' + str(hedge.reject_reason)}")
                                if hedge.passes_liquidity_filter:
                                    roi = round(float((hedge.locked_profit / hedge.total_outlay) * 100), 2)
                                    if 0 < roi < BASEBALL_MAX_ROI:
                                        opportunities.append(_build_opp(x, b["name"], f_opp, hedge, f"Total Runs {line}", poly_side, fiat_side, roi, 0.0, 0.0))

                    elif mt == "spreads":
                        spread = parse_poly_spread(m, h, a)
                        if not spread:
                            continue
                        anchor_team, anchor_point = spread

                        for idx, outcome in enumerate(outs):
                            if idx >= len(toks):
                                continue
                            matched = resolve_outcome_team(str(outcome), h, a)
                            if not matched:
                                continue
                            poly_point = anchor_point if matched == anchor_team else -anchor_point
                            opp_team = opposing_team(matched, h, a)
                            resolved = get_spread_odds_for_team(b, opp_team, -poly_point, h, a)
                            if not resolved:
                                continue
                            resolved_team, f_opp = resolved
                            if resolved_team != opp_team:
                                continue
                            if matched == resolved_team:
                                logger.info(f"   [RL] Validator | Status: ❌ Rejected same-team Poly/fiat run-line pair {matched} {format_point(poly_point)} / {resolved_team} {format_point(-poly_point)}")
                                continue
                            if f_opp:
                                book = clients.get_clob_book(toks[idx])
                                hedge = evaluate_buy_hedge_from_asks(book.get("asks", []), f_opp)
                                poly_price = f"${float(hedge.best_ask):.2f}" if hedge.best_ask else "N/A"
                                poly_side = f"{matched} {format_point(poly_point)}"
                                fiat_side = f"{opp_team} {format_point(-poly_point)}"
                                logger.info(f"   [RL] {b['name']:<10} | {poly_side[:16]:<16} | {b['name']} Opp: {float(f_opp):<5} | Poly Ask: {poly_price:<5} | Status: {'✅' if hedge.passes_liquidity_filter else '❌ ' + str(hedge.reject_reason)}")
                                if hedge.passes_liquidity_filter:
                                    roi = round(float((hedge.locked_profit / hedge.total_outlay) * 100), 2)
                                    if 0 < roi < BASEBALL_MAX_ROI:
                                        opportunities.append(_build_opp(x, b["name"], f_opp, hedge, f"Run Line {format_point(poly_point)}", poly_side, fiat_side, roi, 0.0, 0.0))

        logger.info("\n" + "=" * 80)
        final_alerts = build_baseball_global_alerts(opportunities, fiat_opportunities, limit=3)
        for msg in final_alerts:
            clients.send_telegram_alert(msg)
        logger.info(f"✅ BASEBALL SCAN COMPLETE. Sent {len(final_alerts)} alerts.")
        logger.info("=" * 80)
    finally:
        clients.close()

def _build_fiat_opp(x, b1, b2, o1, o2, m, s1, s2, imp, roi):
    payout = 100.0 / float(imp)
    return FiatArbitrageOpportunity("baseball", x["home"], x["away"], format_to_local(x["time"]), m, b1, s1, float(o1), (payout / float(o1)), b2, s2, float(o2), (payout / float(o2)), float(imp), payout, roi)

def _build_opp(x, b, f_o, hedge, m, p_s, f_s, roi, dt, sp):
    return ArbitrageOpportunity("baseball", x["home"], x["away"], format_to_local(x["time"]), m, p_s, f_s, b, float(f_o), float(hedge.shares), float(hedge.vwap or 0), float(hedge.marginal_price or 0), float(hedge.poly_spend), float(hedge.poly_fees), float(hedge.sportsbook_stake), float(hedge.total_outlay), float(hedge.locked_profit), roi, dt, sp)
