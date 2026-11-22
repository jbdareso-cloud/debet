"""Monthly report on the daily tips (the 4 a day read automatically - TIP_GROUPS[0]). No AI.

For a month: how many tips, won/lost, win rate, profit at the listed odds (1 unit flat per tip), ROI,
and the same per market, with the best market (highest ROI among markets with enough settled tips).
"""

import re

import database as db
import value_engine as ve

MIN_FOR_BEST = 5  # a market needs this many settled tips before it can be called the best


def market_name(tip):
    """'Arsenal to Win' -> 'Home win', 'BTTS Yes' -> 'Both teams to score', etc."""
    home, _, away = (tip.get("match_name") or "").partition(" vs ")
    key = ve.market_key(tip.get("user_proposed_tip") or "", home, away)
    return ve.MARKET_LABELS.get(key) if key else re.sub(r"\s+", " ", tip.get("user_proposed_tip") or "?").strip()


def _summary(tips):
    won = [t for t in tips if t["result"] == "WON"]
    lost = [t for t in tips if t["result"] == "LOST"]
    priced = [t for t in won + lost if t.get("odds")]
    profit = sum(t["odds"] - 1 for t in priced if t["result"] == "WON") - sum(1 for t in priced if t["result"] == "LOST")
    odds = [t["odds"] for t in tips if t.get("odds")]
    settled = len(won) + len(lost)
    avg_odds = sum(odds) / len(odds) if odds else None
    return {"tips": len(tips), "won": len(won), "lost": len(lost), "settled": settled,
            "pending": sum(t["result"] == "PENDING" for t in tips),
            "win_rate": len(won) / settled if settled else None,
            "profit": round(profit, 2), "roi": profit / len(priced) if priced else None,
            "avg_odds": round(avg_odds, 2) if avg_odds else None,
            "break_even": 1 / avg_odds if avg_odds else None}


def months():
    """Months that have daily tips, newest first ('YYYY-MM')."""
    return sorted({str(t["match_date"])[:7] for t in db.list_tips(limit=5000)
                   if t.get("tip_group") == db.TIP_GROUPS[0] and t.get("match_date")}, reverse=True)


def monthly(month):
    tips = [t for t in db.list_tips(limit=5000)
            if t.get("tip_group") == db.TIP_GROUPS[0] and str(t.get("match_date"))[:7] == month]
    by = {}
    for t in tips:
        by.setdefault(market_name(t), []).append(t)
    markets = sorted(({"market": m, **_summary(ts)} for m, ts in by.items()),
                     key=lambda r: (r["roi"] is None, -(r["roi"] or 0), -r["settled"]))
    ranked = [m for m in markets if m["settled"] >= MIN_FOR_BEST and m["roi"] is not None]
    return {"month": month, "total": _summary(tips), "markets": markets,
            "best": ranked[0] if ranked else None, "worst": ranked[-1] if len(ranked) > 1 else None}
