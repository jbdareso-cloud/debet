"""Grade the day's own picks into client tiers and build the accumulator. No AI - uses saved analyses.

Platinum = the pick DeBet rates most likely to win.  Gold = the second most likely.
Silver   = the alternative: the best value pick if one passed the challenge round, otherwise the
           likeliest other market (both models agree) on a different match, with the minimum odds to take.
Accumulator = one leg per tier match, each on that match's likeliest market both models agree on
           (the tipped market or an alternative), skipping legs too short to add anything.

Only picks in the user's own group (TIP_GROUPS[1]) are ever used - never the daily tips site's tips.
"""

import json
import math

import database as db
import value_engine as ve

ACCA_MIN_FAIR_ODDS = 1.25   # an accumulator leg must be worth at least this (fair odds) to add anything
MAX_MODEL_GAP = ve.MAX_MODEL_GAP


def _probs(tip):
    try:
        return json.loads(tip["probs_json"]) if tip.get("probs_json") else None
    except ValueError:
        return None


def _need(p):
    """Lowest odds worth taking for +VALUE_MIN at chance p."""
    return math.ceil((1 + ve.VALUE_MIN) / p * 100) / 100


def _selection(tip, key=None, p=None, odds=None):
    probs = _probs(tip) or {}
    key = key or probs.get("market_key")
    p = p if p is not None else tip.get("p_final")
    listed = odds if odds is not None else (tip.get("odds") if key == probs.get("market_key") else None)
    return {"tip_id": tip["id"], "match": tip["match_name"], "date": str(tip.get("match_date") or "")[:10],
            "market_key": key, "market": ve.MARKET_LABELS.get(key, tip["user_proposed_tip"]) if key else
            tip["user_proposed_tip"], "chance": round(p, 4) if p is not None else None,
            "odds": listed, "min_odds": _need(p) if p else None}


def _agreed_markets(tip):
    """[(chance, key)] for every market both models agree on, likeliest first."""
    probs = _probs(tip)
    if not probs:
        return []
    out = []
    for k, p in probs.get("combined", {}).items():
        g, c = probs["grok"].get(k), probs["claude"].get(k)
        if p and g is not None and c is not None and abs(g - c) <= MAX_MODEL_GAP:
            out.append((p, k))
    return sorted(out, reverse=True)


def own_picks(day):
    """The user's own analysed picks for a match day."""
    return [t for t in db.list_tips(limit=300)
            if t.get("tip_group") == db.TIP_GROUPS[1] and str(t.get("match_date"))[:10] == day
            and t.get("p_final") is not None]


def build_day(day):
    """The tiers and accumulator for a day, or None if there are no analysed own picks."""
    tips = own_picks(day)
    agreed = [t for t in tips if t.get("decision") != "SPLIT"] or tips
    if not agreed:
        return None
    by_chance = lambda ts: sorted(ts, key=lambda t: (t["p_final"], -(t.get("model_gap") or 0)), reverse=True)
    # picks both models agree on come first; the rest only fill a tier that would otherwise be empty
    ranked = by_chance(agreed) + by_chance([t for t in tips if t not in agreed])
    platinum = ranked[0]
    gold = ranked[1] if len(ranked) > 1 else None
    used = {platinum["id"]} | ({gold["id"]} if gold else set())

    # Silver: the best value pick not already used, else the likeliest other market on a different match
    silver = None
    value = [t for t in agreed if t.get("decision") in ("BEST", "VALUE") and t["id"] not in used]
    if value:
        silver = _selection(max(value, key=lambda t: t.get("value") or 0))
    else:
        cands = []
        for t in agreed:
            tipped = (_probs(t) or {}).get("market_key")
            for p, k in _agreed_markets(t):
                if (t["id"] in used and k == tipped) or (k == tipped and t.get("odds")):
                    continue  # already a tier, or already checked at its listed price
                if _need(p) >= ve.MIN_ODDS:
                    cands.append((t["id"] not in used, p, t, k))  # prefer a different match
        if cands:
            _, p, t, k = max(cands, key=lambda c: (c[0], c[1]))
            silver = _selection(t, k, p)

    # Accumulator: one leg per tier match, on its likeliest agreed market worth at least 1.25
    legs, seen = [], set()
    for sel_tip in [platinum, gold] + ([db.get_tip(silver["tip_id"])] if silver else []):
        if not sel_tip or sel_tip["id"] in seen:
            continue
        seen.add(sel_tip["id"])
        options = [(p, k) for p, k in _agreed_markets(sel_tip) if 1 / p >= ACCA_MIN_FAIR_ODDS]
        if options:
            p, k = options[0]
            legs.append(_selection(sel_tip, k, p))
    acca = None
    if len(legs) >= 2:
        chance = math.prod(l["chance"] for l in legs)
        acca = {"legs": legs, "chance": round(chance, 4), "fair_odds": round(1 / chance, 2),
                "min_odds": _need(chance)}  # the +5% margin once, on the combined odds

    return {"day": day, "platinum": _selection(platinum), "gold": _selection(gold) if gold else None,
            "silver": silver, "acca": acca, "picks": len(tips)}


def settle_key(key, score):
    """WON/LOST/VOID for a market key from a 90-minute score like '2-1', or None if unknown."""
    try:
        h, a = (int(x) for x in str(score).replace(":", "-").split("-"))
    except ValueError:
        return None
    total = h + a
    rules = {"home": h > a, "draw": h == a, "away": a > h, "1x": h >= a, "x2": a >= h, "12": h != a,
             "over15": total > 1.5, "over25": total > 2.5, "over35": total > 3.5, "under15": total < 1.5,
             "under25": total < 2.5, "under35": total < 3.5, "btts": h > 0 and a > 0,
             "btts_no": not (h > 0 and a > 0)}
    return ("WON" if rules[key] else "LOST") if key in rules else None


def outcome(sel):
    """Result of one selection from its match's saved score (None while pending)."""
    if not sel:
        return None
    tip = db.get_tip(sel["tip_id"])
    if not tip or not tip.get("final_score"):
        return "VOID" if tip and tip.get("result") == "VOID" else None
    if sel["market_key"] is None:
        return tip.get("result") if tip.get("result") != "PENDING" else None
    return settle_key(sel["market_key"], tip["final_score"])


def acca_outcome(acca):
    if not acca:
        return None
    results = [outcome(l) for l in acca["legs"]]
    if "LOST" in results:
        return "LOST"
    if any(r is None for r in results):
        return None
    return "WON" if any(r == "WON" for r in results) else "VOID"
