"""DeBet value engine: Grok and Claude estimate probabilities independently; code decides the bet.

Per match:
  Step 0  Sonar     - sourced fact brief (the only web search)
  Step 1  Grok      - probabilities for a fixed set of markets, from the brief only
  Step 2  Claude    - the same task, completely independent of Grok
  Code              - combine, measure disagreement, value = probability x odds - 1
Per batch (one day's picks):
  Step 3  Claude    - challenge round on the single best candidate only (may lower, never raise)
  Code              - Best pick of the day, or "No bet today"

Neither model ever sees the tip, the odds or the other model's answer during Steps 1-2.
"""

import json
import re
from datetime import date, datetime, timezone

from debate_engine import (CLAUDE_FALLBACK_MODEL, CLAUDE_MODEL, CREDITS_URL, GROK_FALLBACK_MODEL,
                           GROK_MODEL, INTEL_FALLBACK_MODEL, INTEL_MODEL, MIN_BALANCE_USD,
                           OutOfCreditsError, _chat, _extract_meta, _parse_json_object,
                           get_credit_balance, parse_tip, resolve_match_date, summarize_usage)

# --- Decision rules ----------------------------------------------------------------------------
VALUE_MIN = 0.05        # bet only if probability x odds - 1 is at least +5%
MAX_MODEL_GAP = 0.10    # skip if Grok and Claude are more than 10 points apart on the tip's market
MIN_ODDS = 1.40         # tips priced below this are recorded but not analysed (rarely value; saves cost)

BRIEF_MAX_TOKENS = 500
ESTIMATE_MAX_TOKENS = 350
CHALLENGE_MAX_TOKENS = 300

GROK = [GROK_MODEL, GROK_FALLBACK_MODEL]
CLAUDE = [CLAUDE_MODEL, CLAUDE_FALLBACK_MODEL]

MARKET_LABELS = {
    "home": "Home win", "draw": "Draw", "away": "Away win", "1x": "Home or draw (1X)",
    "x2": "Draw or away (X2)", "12": "Home or away (12)", "over15": "Over 1.5 goals",
    "over25": "Over 2.5 goals", "over35": "Over 3.5 goals", "under15": "Under 1.5 goals",
    "under25": "Under 2.5 goals", "under35": "Under 3.5 goals", "btts": "Both teams to score",
    "btts_no": "Both teams to score - No",
}

# --- Prompts --------------------------------------------------------------------------------
BRIEF_SYSTEM = (
    "You are a football match researcher with live web search. Today is {today}. Write a short, "
    "factual brief. Every injury, suspension or line-up claim must name its source and date. "
    "Anything you cannot confirm goes under 'Unknown'. Never guess. Do not look up or mention betting odds."
)

BRIEF_USER = """Fixture: {match}
Expected date: on or around {expected_date}.

FIRST confirm the exact kickoff date from live fixture data (official site, Flashscore, Sofascore, BBC, ESPN).
If the teams meet more than once, use the meeting closest to the expected date that has not already finished.
Start your reply with exactly one line of JSON:
META: {{"competition": "<name or null>", "match_date": "<YYYY-MM-DD or null>", "kickoff_time": "<HH:MM with timezone or null>", "match_date_confirmed": <true/false>, "fixture_source": "<site or null>"}}

Then, max ~250 words, terse bullets:
1. Absences: confirmed injuries/suspensions and doubts for each team (player, role, source, date).
2. Expected line-ups or key rotation news, if published.
3. Form, last 6 matches each: results, goals for/against, and xG for/against if available.
4. Home form of the home side, away form of the away side.
5. Context: league position, motivation, days of rest, next important fixture.
6. Unknown: what you could not confirm."""

ESTIMATE_SYSTEM = (
    "You are a careful football forecaster. Estimate full-time (90 minutes + stoppage) probabilities "
    "using ONLY the brief and general football knowledge. Be calibrated: in a typical league the home "
    "side wins ~45%, draws ~26%, away ~29%; Over 2.5 and both-teams-to-score are each ~50%. Move away "
    "from these only as far as the evidence in the brief justifies. Unconfirmed information should move "
    "you less than confirmed information. You are never told any betting price. Reply with JSON only."
)

ESTIMATE_USER = """Match: {match} (home team first)
Date: {match_date}
{extra}
BRIEF:
{brief}

Return exactly this JSON (probabilities as decimals between 0 and 1):
{{"home": 0.00, "draw": 0.00, "away": 0.00, "over15": 0.00, "over25": 0.00, "over35": 0.00, "btts": 0.00,{custom_key}
 "reasons": ["<the 2 facts that moved you most>", "..."],
 "missing": ["<important information the brief did not have>"]}}
Rules: home + draw + away = 1. over15 >= over25 >= over35."""

CHALLENGE_SYSTEM = (
    "You are the final checker before a bet. Your only job is to catch concrete problems: facts the "
    "estimates misread, an important absence or rotation they missed, or reasoning that contradicts the "
    "brief. Ordinary football uncertainty is NOT a problem. You may keep or LOWER the probability, never "
    "raise it. You are never told the price. Reply with JSON only."
)

CHALLENGE_USER = """Match: {match} ({match_date})
Market being checked: {market}

BRIEF:
{brief}

ESTIMATE A (probability {p_a:.0%}), reasons: {reasons_a}
ESTIMATE B (probability {p_b:.0%}), reasons: {reasons_b}
Combined probability: {p:.0%}

Return exactly: {{"p": <your probability for this market, at most {p:.2f}>, "issues": ["<concrete problem>", "..."], "ok": <true if no concrete problem>}}"""


# --- Market handling ----------------------------------------------------------------------------
def market_key(market, home, away):
    """Map a parsed tip market to a key the models price, or None (then it's priced as a custom item)."""
    s = re.sub(r"\s+", " ", market or "").strip().lower()
    h, a = home.strip().lower(), away.strip().lower()
    m = re.fullmatch(r"(over|under) ([123])\.5 goals", s)
    if m:
        return f"{m.group(1)}{m.group(2)}5"
    if s == "btts yes":
        return "btts"
    if s == "btts no":
        return "btts_no"
    if s == "draw":
        return "draw"
    m = re.fullmatch(r"double chance (1x|x2|12)", s)
    if m:
        return m.group(1)
    m = re.fullmatch(r"(.+) to win", s)
    if m and m.group(1) in (h, a):
        return "home" if m.group(1) == h else "away"
    return None


def _clip(p):
    try:
        p = float(p)
    except (TypeError, ValueError):
        return None
    return min(max(p, 0.01), 0.99)


def complete_probs(raw):
    """Clean one model's JSON into a full, consistent market set (normalised 1X2, ordered overs,
    derived unders/double chance/BTTS No). Returns dict or raises ValueError."""
    raw = raw if isinstance(raw, dict) else {}
    h, d, a = (_clip(raw.get(k)) for k in ("home", "draw", "away"))
    if None in (h, d, a):
        raise ValueError("missing home/draw/away probabilities")
    total = h + d + a
    h, d, a = h / total, d / total, a / total
    o15, o25, o35, btts = (_clip(raw.get(k)) for k in ("over15", "over25", "over35", "btts"))
    if None in (o15, o25, o35, btts):
        raise ValueError("missing goals/BTTS probabilities")
    o25 = min(o25, o15)
    o35 = min(o35, o25)
    p = {"home": h, "draw": d, "away": a, "1x": h + d, "x2": d + a, "12": h + a,
         "over15": o15, "over25": o25, "over35": o35, "under15": 1 - o15, "under25": 1 - o25,
         "under35": 1 - o35, "btts": btts, "btts_no": 1 - btts}
    if raw.get("custom") is not None and _clip(raw.get("custom")) is not None:
        p["custom"] = _clip(raw["custom"])
    return {k: round(v, 4) for k, v in p.items()}


def _clean_list(value, limit=3):
    return [str(x).strip() for x in (value or []) if str(x).strip()][:limit] if isinstance(value, list) else []


# --- One match ----------------------------------------------------------------------------------
def analyze_tip(tip_text, extra_notes="", default_match_date=None):
    """Brief + two independent estimates for one tip. Returns a result dict (see _analyze).
    If it raises, the exception carries `debate_cost` with whatever was already spent."""
    usage = []
    try:
        result = _analyze(tip_text, extra_notes, default_match_date, usage)
    except Exception as exc:
        exc.debate_cost = summarize_usage(usage)
        raise
    result["cost"] = summarize_usage(usage)
    return result


def _estimate(models, label, match, match_date, brief, extra, custom, usage):
    custom_key = f' "custom": 0.00,  /* probability of: {custom} */' if custom else ""
    text, model = _chat(models, [
        {"role": "system", "content": ESTIMATE_SYSTEM},
        {"role": "user", "content": ESTIMATE_USER.format(match=match, match_date=match_date or "unknown",
                                                         extra=extra, brief=brief, custom_key=custom_key)},
    ], temperature=0.2, max_tokens=ESTIMATE_MAX_TOKENS, usage=usage, label=label)
    raw = _parse_json_object(re.sub(r"/\*.*?\*/", "", text))
    if raw is None:
        raise RuntimeError(f"{label} did not return JSON: {text[:200]!r}")
    return {"model": model, "probs": complete_probs(raw), "reasons": _clean_list(raw.get("reasons")),
            "missing": _clean_list(raw.get("missing"))}


def _analyze(tip_text, extra_notes, default_match_date, usage):
    tip = parse_tip(tip_text)
    home, away, market = tip["home_team"], tip["away_team"], tip["market"]
    key = market_key(market, home, away)
    started = datetime.now(timezone.utc)
    if isinstance(default_match_date, date):
        default_match_date = default_match_date.isoformat()
    base = {**tip, "market_key": key, "analyzed_at": started.isoformat(timespec="seconds")}

    if tip["odds"] is not None and tip["odds"] < MIN_ODDS:
        return {**base, "decision": "SHORT PRICE", "skipped": True,
                "note": f"Odds {tip['odds']:.2f} are below {MIN_ODDS:.2f} - recorded, not analysed."}

    balance = get_credit_balance()
    if balance is not None and balance < MIN_BALANCE_USD:
        raise OutOfCreditsError(f"OpenRouter balance is ${balance:.2f}. Top up at {CREDITS_URL}")

    extra = f"Notes: {extra_notes.strip()}\n" if extra_notes and extra_notes.strip() else ""
    expected = default_match_date or f"{started.date().isoformat()} (today) or the next scheduled meeting"
    brief_msgs = [{"role": "system", "content": BRIEF_SYSTEM.format(today=started.date().isoformat())},
                  {"role": "user", "content": BRIEF_USER.format(match=tip["match_name"], expected_date=expected)}]
    try:
        brief_raw, brief_model = _chat(INTEL_MODEL, brief_msgs, temperature=0.2, max_tokens=BRIEF_MAX_TOKENS,
                                       usage=usage, label="Brief (Sonar)")
    except OutOfCreditsError:
        raise
    except RuntimeError:
        brief_raw, brief_model = _chat(INTEL_FALLBACK_MODEL, brief_msgs, web_search=True, temperature=0.2,
                                       max_tokens=BRIEF_MAX_TOKENS, usage=usage, label="Brief fallback (web)")
    meta, brief = _extract_meta(brief_raw)
    meta = meta or {}
    match_date, date_source, date_warning = resolve_match_date(meta, default_match_date, started.date())

    custom = None if key else market
    grok = _estimate(GROK, "Estimate (Grok)", tip["match_name"], match_date, brief, extra, custom, usage)
    claude = _estimate(CLAUDE, "Estimate (Claude)", tip["match_name"], match_date, brief, extra, custom, usage)

    k = key or "custom"
    p_g, p_c = grok["probs"].get(k), claude["probs"].get(k)
    if p_g is None or p_c is None:
        raise RuntimeError(f"A model did not price the market '{market}'.")
    p_final = round((p_g + p_c) / 2, 4)
    gap = round(abs(p_g - p_c), 4)
    value = round(p_final * tip["odds"] - 1, 4) if tip["odds"] else None
    combined = {m: round((grok["probs"][m] + claude["probs"][m]) / 2, 4) for m in grok["probs"]
                if m in claude["probs"] and m != "custom"}

    if value is None:
        decision = "NO ODDS"
    elif gap > MAX_MODEL_GAP:
        decision = "SPLIT"
    elif value >= VALUE_MIN:
        decision = "VALUE"
    else:
        decision = "NO VALUE"

    return {**base, "skipped": False, "brief": brief, "brief_model": brief_model, "match_date": match_date,
            "match_date_source": date_source, "date_warning": date_warning,
            "competition": meta.get("competition") or None, "grok": grok, "claude": claude,
            "combined": combined, "p_grok": p_g, "p_claude": p_c, "p_final": p_final, "model_gap": gap,
            "value": value, "decision": decision, "challenge": None}


# --- One day: pick the best -------------------------------------------------------------------------
def choose_best(results):
    """Challenge the single best VALUE candidate and mark it BEST if it survives.

    `results` are analyze_tip() dicts (mutated in place). Returns (index or None, message, cost summary).
    """
    usage = []
    cands = [(i, r) for i, r in enumerate(results) if r and r.get("decision") == "VALUE"]
    if not cands:
        return None, "No bet today - no tip passed the value check.", summarize_usage(usage)
    i, best = max(cands, key=lambda c: c[1]["value"])
    label = MARKET_LABELS.get(best["market_key"], best["market"])
    try:
        text, model = _chat(CLAUDE, [
            {"role": "system", "content": CHALLENGE_SYSTEM},
            {"role": "user", "content": CHALLENGE_USER.format(
                match=best["match_name"], match_date=best.get("match_date") or "unknown", market=label,
                brief=best["brief"], p_a=best["p_grok"], reasons_a="; ".join(best["grok"]["reasons"]) or "-",
                p_b=best["p_claude"], reasons_b="; ".join(best["claude"]["reasons"]) or "-", p=best["p_final"])},
        ], temperature=0.0, max_tokens=CHALLENGE_MAX_TOKENS, usage=usage, label="Challenge (Claude)")
        raw = _parse_json_object(text) or {}
    except RuntimeError as exc:
        best["challenge"] = {"ok": False, "issues": [f"Challenge failed: {exc}"], "p": best["p_final"]}
        best["decision"] = "VALUE"
        return None, "No bet today - the challenge round could not run, so nothing was confirmed.", \
            summarize_usage(usage)

    p_new = _clip(raw.get("p"))
    p_new = min(p_new, best["p_final"]) if p_new is not None else best["p_final"]
    issues = _clean_list(raw.get("issues"), limit=4)
    ok = raw.get("ok") is True or str(raw.get("ok")).lower() == "true"
    best["challenge"] = {"ok": ok, "issues": issues, "p": round(p_new, 4), "model": model}
    best["p_final"] = round(p_new, 4)
    best["value"] = round(p_new * best["odds"] - 1, 4)
    if best["value"] >= VALUE_MIN:
        best["decision"] = "BEST"
        return i, f"Best pick: {best['match_name']} - {best['market']} @ {best['odds']:.2f} " \
                  f"(value {best['value']:+.0%}).", summarize_usage(usage)
    best["decision"] = "NO VALUE"
    return None, (f"No bet today - the challenge lowered {best['match_name']} to {p_new:.0%}, "
                  f"so its value fell to {best['value']:+.0%}."), summarize_usage(usage)


# --- Database fields --------------------------------------------------------------------------------
def tip_fields(r, group, slot, default_date=None):
    """Keyword args for database.save_tip from an analyze_tip() result."""
    approved = r.get("decision") in ("BEST", "VALUE")
    fields = {"match_name": r["match_name"], "user_proposed_tip": r["market"], "odds": r["odds"],
              "match_date": r.get("match_date") or default_date, "competition": r.get("competition"),
              "tip_group": group, "slot_number": slot, "decision": r.get("decision")}
    if r.get("skipped"):
        fields.update(ai_verdict="AVOID", full_debate_log=r.get("note"))
        return fields
    ch = r.get("challenge")
    log = "\n\n".join(x for x in [
        f"=== BRIEF ({r['brief_model']}) ===\n{r['brief']}",
        f"=== GROK ({r['grok']['model']}) - {r['p_grok']:.0%} ===\nReasons: " + "; ".join(r["grok"]["reasons"])
        + ("\nMissing: " + "; ".join(r["grok"]["missing"]) if r["grok"]["missing"] else ""),
        f"=== CLAUDE ({r['claude']['model']}) - {r['p_claude']:.0%} ===\nReasons: " + "; ".join(r["claude"]["reasons"])
        + ("\nMissing: " + "; ".join(r["claude"]["missing"]) if r["claude"]["missing"] else ""),
        (f"=== CHALLENGE ({ch.get('model', 'Claude')}) ===\n" + ("No concrete problem found." if ch["ok"] else
         "Issues: " + "; ".join(ch["issues"] or ["-"])) + f"\nProbability after challenge: {ch['p']:.0%}") if ch else "",
    ] if x)
    fields.update(
        ai_verdict="PLAY AS IS" if approved else "AVOID",
        p_grok=r["p_grok"], p_claude=r["p_claude"], p_final=r["p_final"], value=r["value"],
        model_gap=r["model_gap"], full_debate_log=log,
        risk_factor="; ".join((ch or {}).get("issues") or r["claude"]["missing"][:1] or []) or None,
        edge_summary="; ".join(r["grok"]["reasons"][:1] + r["claude"]["reasons"][:1]) or None,
        challenge_note=("OK" if ch["ok"] else "; ".join(ch["issues"])) if ch else None,
        probs_json=json.dumps({"grok": r["grok"]["probs"], "claude": r["claude"]["probs"],
                               "combined": r["combined"], "market_key": r["market_key"]}),
    )
    return fields
