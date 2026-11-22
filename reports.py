"""Monthly report on the daily tips (the 4 a day read automatically - TIP_GROUPS[0]). No AI.

For a month: how many tips, won/lost, win rate, profit at the listed odds (1 unit flat per tip), ROI,
and the same per market, with the best market (highest ROI among markets with enough settled tips).
"""

import re
from datetime import date, timedelta

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
    return {"month": month, **_report(tips)}


def _report(tips):
    by = {}
    for t in tips:
        by.setdefault(market_name(t), []).append(t)
    markets = sorted(({"market": m, **_summary(ts)} for m, ts in by.items()),
                     key=lambda r: (r["roi"] is None, -(r["roi"] or 0), -r["settled"]))
    ranked = [m for m in markets if m["settled"] >= MIN_FOR_BEST and m["roi"] is not None]
    return {"total": _summary(tips), "markets": markets,
            "best": ranked[0] if ranked else None, "worst": ranked[-1] if len(ranked) > 1 else None}


# --- Weekly (Monday-Sunday): daily tips + DeBet's own picks ------------------------------------------
MIN_FOR_BEST_WEEK = 3   # a week is short: a market needs 3+ settled tips to be called the best


def weeks():
    """Mondays of weeks that have daily tips, newest first (date objects)."""
    days = {date.fromisoformat(str(t["match_date"])[:10]) for t in db.list_tips(limit=5000)
            if t.get("tip_group") == db.TIP_GROUPS[0] and t.get("match_date")}
    return sorted({d - timedelta(days=d.weekday()) for d in days}, reverse=True)


def weekly(monday):
    """The week Monday..Sunday: daily tips (overall, per market, best market) and DeBet's picks per tier."""
    import client_tiers as ct  # here: client_tiers imports database too
    sunday = monday + timedelta(days=6)
    in_week = lambda t: monday.isoformat() <= str(t.get("match_date"))[:10] <= sunday.isoformat()
    daily = [t for t in db.list_tips(limit=5000) if t.get("tip_group") == db.TIP_GROUPS[0] and in_week(t)]
    rep = _report(daily)
    if rep["best"] and rep["best"]["settled"] < MIN_FOR_BEST_WEEK:
        rep["best"] = None
    if not rep["best"]:  # weeks are short: allow 3+ settled tips
        ranked = [m for m in rep["markets"] if m["settled"] >= MIN_FOR_BEST_WEEK and m["roi"] is not None]
        rep["best"] = ranked[0] if ranked else None
    tiers = {k: {"won": 0, "lost": 0, "profit": 0.0, "picks": 0} for k in ("platinum", "gold", "silver", "acca")}
    for i in range(7):
        day = (monday + timedelta(days=i)).isoformat()
        saved = db.get_client_day(day)
        picks = saved if saved and saved.get("published_at") else ct.build_day(day)
        if not picks:
            continue
        for k in tiers:
            sel = picks.get(k)
            if not sel:
                continue
            res = ct.acca_outcome(sel) if k == "acca" else ct.outcome(sel)
            odds = sel.get("min_odds") if k == "acca" else (sel.get("odds") or sel.get("min_odds"))
            tiers[k]["picks"] += 1
            if res == "WON":
                tiers[k]["won"] += 1
                tiers[k]["profit"] += (odds or 1) - 1
            elif res == "LOST":
                tiers[k]["lost"] += 1
                tiers[k]["profit"] -= 1
    singles = [tiers[k] for k in ("platinum", "gold", "silver")]
    ai_total = {"won": sum(t["won"] for t in singles), "lost": sum(t["lost"] for t in singles),
                "profit": round(sum(t["profit"] for t in singles), 2)}
    today = date.today()
    return {"monday": monday, "sunday": sunday, "complete": sunday < today, "daily": rep,
            "tiers": {k: {**v, "profit": round(v["profit"], 2)} for k, v in tiers.items()}, "ai_total": ai_total}
