from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, getcontext
from typing import Optional

getcontext().prec = 28
ONE = Decimal("1")


def q_from_decimal(odds) -> Decimal:
    o = Decimal(str(odds))
    if o <= 1:
        raise ValueError("decimal odds must be > 1")
    return ONE / o


def _valid_decimal_odds(odds) -> Optional[Decimal]:
    try:
        value = Decimal(str(odds))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return value if value > 1 else None


def q_from_polymarket(price, fee_rate="0.03") -> Decimal:
    p, r = Decimal(str(price)), Decimal(str(fee_rate))
    return p + r * p * (ONE - p)


def q_from_kalshi(price, fee_rate="0.07") -> Decimal:
    p, r = Decimal(str(price)), Decimal(str(fee_rate))
    return p + r * p * (ONE - p)


@dataclass(frozen=True)
class Leg:
    outcome: str
    venue: str
    q: Decimal
    quote: Decimal
    max_payout: Decimal


@dataclass(frozen=True)
class ArbResult:
    is_arb: bool
    booksum: Decimal
    roi: Decimal
    total_outlay: Decimal
    guaranteed_payout: Decimal
    net_profit: Decimal
    stakes: dict
    legs: dict
    reason: Optional[str] = None


def solve_nway(partition, candidates, bankroll, min_roi="0.0", max_roi="0.15") -> ArbResult:
    bankroll_d = Decimal(str(bankroll))
    best: dict[str, Leg] = {}
    for candidate in candidates:
        if candidate.outcome not in best or candidate.q < best[candidate.outcome].q:
            best[candidate.outcome] = candidate

    missing = [outcome for outcome in partition if outcome not in best]
    if missing:
        return ArbResult(
            False, Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"),
            Decimal("0"), {}, {}, reason=f"incomplete_partition: missing {missing}"
        )

    booksum = sum((best[outcome].q for outcome in partition), Decimal("0"))
    roi = (ONE / booksum) - ONE
    if booksum >= ONE:
        return ArbResult(False, booksum, roi, Decimal("0"), Decimal("0"), Decimal("0"), {}, best, reason="no edge")
    if roi < Decimal(str(min_roi)):
        return ArbResult(False, booksum, roi, Decimal("0"), Decimal("0"), Decimal("0"), {}, best, reason="below min ROI")
    if roi > Decimal(str(max_roi)):
        return ArbResult(False, booksum, roi, Decimal("0"), Decimal("0"), Decimal("0"), {}, best, reason="sanity cap")

    cap_payout = min(best[outcome].max_payout for outcome in partition)
    bankroll_payout = bankroll_d / booksum
    payout = min(cap_payout, bankroll_payout)
    if payout <= 0:
        return ArbResult(False, booksum, roi, Decimal("0"), Decimal("0"), Decimal("0"), {}, best, reason="no depth")

    stakes = {outcome: best[outcome].q * payout for outcome in partition}
    outlay = sum(stakes.values(), Decimal("0"))
    return ArbResult(True, booksum, roi, outlay, payout, payout - outlay, stakes, best)


def fiat_fiat_legs_from_books(game: dict, bookies: list, bankroll="1000", max_roi="0.15"):
    results = []
    home, away = game["home"], game["away"]

    ml = {"Home": None, "Draw": None, "Away": None}
    src: dict[str, str] = {}
    dc = {"1X": None, "X2": None, "12": None}
    dc_src: dict[str, str] = {}
    for book in bookies:
        for selection, odds in book.get("h2h", {}).items():
            odds_d = _valid_decimal_odds(odds)
            if odds_d is None:
                continue
            if selection == home:
                key = "Home"
            elif selection == away:
                key = "Away"
            elif str(selection).lower() == "draw":
                key = "Draw"
            else:
                continue
            if ml[key] is None or odds_d > ml[key]:
                ml[key] = odds_d
                src[key] = book["name"]

        for selection, odds in book.get("double_chance", {}).items():
            if selection not in dc:
                continue
            odds_d = _valid_decimal_odds(odds)
            if odds_d is None:
                continue
            if dc[selection] is None or odds_d > dc[selection]:
                dc[selection] = odds_d
                dc_src[selection] = book["name"]

    if all(ml[key] is not None for key in ml):
        legs = [
            Leg(key, src[key], q_from_decimal(ml[key]), ml[key], Decimal("1e9"))
            for key in ml
        ]
        result = solve_nway(["Home", "Draw", "Away"], legs, bankroll, max_roi=max_roi)
        if result.is_arb:
            results.append(("3-WAY ML", result))

    dc_pairs = (
        ("DOUBLE CHANCE 1X + AWAY ML", "1X", "Away"),
        ("DOUBLE CHANCE X2 + HOME ML", "X2", "Home"),
        ("DOUBLE CHANCE 12 + DRAW ML", "12", "Draw"),
    )
    for label, dc_key, ml_key in dc_pairs:
        if dc[dc_key] is None or ml[ml_key] is None:
            continue
        legs = [
            Leg(dc_key, dc_src[dc_key], q_from_decimal(dc[dc_key]), dc[dc_key], Decimal("1e9")),
            Leg(ml_key, src[ml_key], q_from_decimal(ml[ml_key]), ml[ml_key], Decimal("1e9")),
        ]
        result = solve_nway([dc_key, ml_key], legs, bankroll, max_roi=max_roi)
        if result.is_arb:
            results.append((label, result))

    lines: dict = {}
    for book in bookies:
        for line, sides in book.get("totals", {}).items():
            row = lines.setdefault(line, {"Over": None, "Under": None, "src": {}})
            for raw, canonical in (("over", "Over"), ("under", "Under")):
                if raw in sides:
                    odds_d = _valid_decimal_odds(sides[raw])
                    if odds_d is None:
                        continue
                    if row[canonical] is None or odds_d > row[canonical]:
                        row[canonical] = odds_d
                        row["src"][canonical] = book["name"]

    for line, row in lines.items():
        if row["Over"] is not None and row["Under"] is not None:
            legs = [
                Leg("Over", row["src"]["Over"], q_from_decimal(row["Over"]), row["Over"], Decimal("1e9")),
                Leg("Under", row["src"]["Under"], q_from_decimal(row["Under"]), row["Under"], Decimal("1e9")),
            ]
            result = solve_nway(["Over", "Under"], legs, bankroll, max_roi=max_roi)
            if result.is_arb:
                results.append((f"TOTALS {line}", result))

    btts = {"Yes": None, "No": None, "src": {}}
    for book in bookies:
        for raw, canonical in (("yes", "Yes"), ("no", "No")):
            if raw in book.get("btts", {}):
                odds_d = _valid_decimal_odds(book["btts"][raw])
                if odds_d is None:
                    continue
                if btts[canonical] is None or odds_d > btts[canonical]:
                    btts[canonical] = odds_d
                    btts["src"][canonical] = book["name"]

    if btts["Yes"] is not None and btts["No"] is not None:
        legs = [
            Leg("Yes", btts["src"]["Yes"], q_from_decimal(btts["Yes"]), btts["Yes"], Decimal("1e9")),
            Leg("No", btts["src"]["No"], q_from_decimal(btts["No"]), btts["No"], Decimal("1e9")),
        ]
        result = solve_nway(["Yes", "No"], legs, bankroll, max_roi=max_roi)
        if result.is_arb:
            results.append(("BTTS", result))

    return results


def format_nway_alert(game: dict, label: str, result: ArbResult) -> str:
    lines = [
        "SOCCER FIAT-vs-FIAT ARB",
        f"MATCHUP: {game['home']} vs {game['away']}",
        f"DATE: {game.get('time', '')[:16]}",
        f"MARKET: {label}",
        f"ROI: {result.roi:.2%} | PROFIT: ${result.net_profit:.2f} on ${result.total_outlay:.2f}",
        "",
    ]
    for outcome in result.stakes:
        leg = result.legs[outcome]
        lines.append(f"Bet ${result.stakes[outcome]:.2f} on '{outcome}' at {leg.venue} ({leg.quote})")
    lines.append("")
    lines.append(f"GUARANTEED PAYOUT: ${result.guaranteed_payout:.2f}")
    return "\n".join(lines)
