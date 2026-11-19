"""Automatic result verification for PENDING tips using Grok with live web search."""

import os
import re
from datetime import date, datetime, timedelta, timezone

import database as db
from debate_engine import _chat, _extract_json_line, format_cost, summarize_usage

VERIFIER_MODEL = "perplexity/sonar"  # built-in web search; called with web_search=False (no plugin)
VERIFIER_MAX_TOKENS = 450
CHECK_INTERVAL = timedelta(hours=3)
LAST_CHECKED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "last_checked.txt")

_SCORE_RE = re.compile(r"^\s*(\d{1,2})\s*[-:]\s*(\d{1,2})\s*$")

VERIFY_SYSTEM = (
    "You are a strict football results checker with live web search. Today is {today}. "
    "Use official or reputable sources (league site, BBC, ESPN, Flashscore, Sofascore). "
    "Never guess: if you cannot confirm the match has finished, report NOT_PLAYED."
)

VERIFY_USER = """Check this match and settle the bet(s) on it:
Match: {match_name}  (home team first)
Competition: {competition}
Scheduled date: {match_date}
Bet 1 (original tip): {tip}
Bet 2 (alternative market): {alt_tip}

1. Has this match finished? Find the official full-time score (90 minutes plus stoppage time,
   excluding extra time and penalties unless a bet explicitly says otherwise).
2. Settle EACH bet strictly against that one score:
   - WON if the bet's condition is met, LOST if not.
   - VOID only if the match was abandoned/cancelled/awarded, or the market rules void the bet
     (e.g. Draw No Bet ending in a draw, a non-starting player in a player market).
   - If the match is postponed, still upcoming, in progress, or you cannot confirm the score,
     use status NOT_PLAYED.
   - For markets that need more than the score (corners, cards, half-time), use the official match stats.

Be terse: one line with the source and score, then end with exactly one line (no code fence):
RESULT_JSON: {{"status": "FINISHED" or "NOT_PLAYED", "final_score": "home-away e.g. 2-1", "outcome": "WON" or "LOST" or "VOID", "alt_outcome": "WON" or "LOST" or "VOID" or null}}"""


# ---------------------------------------------------------------------------
# Throttle (stored in the database, so a restarted cloud app doesn't re-check and pay again)
# ---------------------------------------------------------------------------
LAST_CHECKED_KEY = "LAST_RESULT_CHECK"


def get_last_checked():
    value = db.get_setting(LAST_CHECKED_KEY)
    if not value:  # older installs kept it in last_checked.txt
        try:
            with open(LAST_CHECKED_FILE, encoding="utf-8") as fh:
                value = fh.read().strip()
        except OSError:
            return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _set_last_checked(when):
    db.update_setting(LAST_CHECKED_KEY, when.isoformat(timespec="seconds"))


def is_check_due(now=None):
    last = get_last_checked()
    now = now or datetime.now(timezone.utc)
    return last is None or now - last >= CHECK_INTERVAL


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------
def _match_could_be_over(tip, today):
    """Skip tips whose match date is in the future (saves credits). Unknown dates are checked."""
    try:
        return date.fromisoformat(str(tip.get("match_date"))[:10]) <= today
    except ValueError:
        return True


def _fmt_bet(market, odds):
    return f"{market} @ {odds:.2f}" if market and isinstance(odds, (int, float)) else (market or "")


def settle_market(market, score, home, away):
    """Settle a full-time-score market locally. Returns WON/LOST/VOID, or None if the market
    needs more than the final score (corners, cards, halves, players, Asian quarter lines...)."""
    m = _SCORE_RE.match(score or "")
    if not m or not market:
        return None
    h, a = int(m.group(1)), int(m.group(2))
    total = h + a
    s = re.sub(r"\s+", " ", market).strip().lower()
    home_l, away_l = home.strip().lower(), away.strip().lower()
    if re.search(r"half|\b1h\b|\b2h\b|\bht\b|corner|card|booking|player|shot|scorer|team total|minute", s):
        return None
    s = re.sub(r"\s*\((?:[^)]*\bor\b[^)]*|[^)]*draw[^)]*)\)$", "", s)  # '1X (Arsenal or Draw)'

    def won_if(cond):
        return "WON" if cond else "LOST"

    mm = re.fullmatch(r"(over|under) (\d+(?:\.\d+)?)(?: goals?)?", s)
    if mm:
        line = float(mm.group(2))
        if line * 2 != int(line * 2):  # quarter lines settle half-won/half-lost
            return None
        if total == line:
            return "VOID"
        return won_if(total > line if mm.group(1) == "over" else total < line)
    if s in ("btts yes", "btts", "both teams to score", "both teams to score yes", "gg"):
        return won_if(h > 0 and a > 0)
    if s in ("btts no", "both teams to score no", "ng"):
        return won_if(not (h > 0 and a > 0))
    mm = re.fullmatch(r"(?:double chance )?(1x|x2|12)", s)
    if mm:
        return won_if({"1x": h >= a, "x2": a >= h, "12": h != a}[mm.group(1)])
    if s in ("draw", "x"):
        return won_if(h == a)
    mm = re.fullmatch(r"correct score (\d+)-(\d+)", s)
    if mm:
        return won_if((h, a) == (int(mm.group(1)), int(mm.group(2))))

    def side(team):
        return "home" if team == home_l else "away" if team == away_l else None

    mm = re.fullmatch(r"(.+?) (?:to win|win)", s)
    if mm and side(mm.group(1)):
        return won_if(h > a if side(mm.group(1)) == "home" else a > h)
    mm = re.fullmatch(r"draw no bet (.+)|(.+?) (?:draw no bet|dnb)", s)
    if mm and side(mm.group(1) or mm.group(2)):
        if h == a:
            return "VOID"
        return won_if(h > a if side(mm.group(1) or mm.group(2)) == "home" else a > h)
    return None


def check_tip_result(tip, usage=None):
    """Look up one match's final score once and settle both the original and the alternative market.

    Returns {"status": "NOT_PLAYED"} or {"status": "FINISHED", "final_score", "outcome", "alt_outcome",
    "notes"}. Common markets are settled locally from the verified score; others use the model's call.
    Raises ValueError on an unusable reply. If `usage` is a list, the call's token/cost entry is appended.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    has_alt = bool(tip.get("alt_market"))
    text, _ = _chat(VERIFIER_MODEL, [
        {"role": "system", "content": VERIFY_SYSTEM.format(today=today)},
        {"role": "user", "content": VERIFY_USER.format(
            match_name=tip["match_name"],
            competition=tip.get("competition") or "unknown",
            match_date=tip.get("match_date") or "unknown",
            tip=_fmt_bet(tip["user_proposed_tip"], tip.get("odds")),
            alt_tip=_fmt_bet(tip.get("alt_market"), tip.get("alt_odds")) if has_alt else "none - use null",
        )},
    ], web_search=False, temperature=0.0, max_tokens=VERIFIER_MAX_TOKENS,  # Sonar searches natively
        usage=usage, label=f"Verify {tip['match_name']}")

    data, _ = _extract_json_line(text, "RESULT_JSON")
    if not data:
        raise ValueError(f"No RESULT_JSON in model reply: {text[-300:]!r}")

    status = str(data.get("status", "")).strip().upper()
    if status != "FINISHED":
        return {"status": "NOT_PLAYED"}

    def outcome_of(key):
        value = str(data.get(key) or "").strip().upper()
        return value if value in ("WON", "LOST", "VOID") else None

    score = str(data.get("final_score", "")).strip()
    m = _SCORE_RE.match(score)
    score = f"{m.group(1)}-{m.group(2)}" if m else score
    if not m and outcome_of("outcome") != "VOID":  # abandoned/cancelled matches may have no score
        raise ValueError(f"Invalid final score {score!r}")

    home, _, away = tip["match_name"].partition(" vs ")
    notes = []

    def settle(market, model_outcome, label):
        local = settle_market(market, score, home, away) if m else None
        if local and model_outcome and local != model_outcome:
            notes.append(f"{label}: model said {model_outcome}, score {score} settles it {local}")
        return local or model_outcome

    outcome = settle(tip["user_proposed_tip"], outcome_of("outcome"), "original")
    if not outcome and tip.get("result", "PENDING") == "PENDING":
        raise ValueError(f"Invalid outcome {data.get('outcome')!r}")
    alt_outcome = settle(tip["alt_market"], outcome_of("alt_outcome"), "alternative") if has_alt else None
    if has_alt and not alt_outcome:
        notes.append("alternative market could not be settled - left PENDING for manual review")
    return {"status": "FINISHED", "final_score": score, "outcome": outcome,
            "alt_outcome": alt_outcome, "notes": notes}


def verify_pending_tips(force=False):
    """Verify every unsettled tip (published or not, original and AI alternative market).

    Without force, does nothing if the last check was under 3 hours ago
    (use this on app startup). With force=True it always runs (admin button).
    Returns a summary dict.
    """
    now = datetime.now(timezone.utc)
    if not force and not is_check_due(now):
        last = get_last_checked()
        return {"skipped": True, "reason": "checked recently", "last_checked": last.isoformat(),
                "next_check_due": (last + CHECK_INTERVAL).isoformat()}

    # Stamp first so overlapping startups (e.g. app reloads) don't trigger duplicate paid runs.
    _set_last_checked(now)

    summary = {"skipped": False, "checked": 0, "settled": [], "not_played": [],
               "future": [], "errors": [], "last_checked": now.isoformat(timespec="seconds")}
    usage = []
    try:
        _verify_tips(summary, now, usage)
    finally:
        # db.update_tip_result already re-grades slips per tip; this full sweep is a cheap safety net
        # so every user's bet slips match the latest tip results after a verification run.
        try:
            summary["bets_regraded"] = db.recalculate_user_bets()
        except Exception as exc:
            summary["errors"].append({"tip_id": None, "match": "user bet slips", "error": str(exc)})
        summary["cost"] = summarize_usage(usage)
        print(f"[DeBet cost] Result verification ({summary['checked']} matches): "
              f"{format_cost(summary['cost'])}", flush=True)
    return summary


def _verify_tips(summary, now, usage):
    for tip in db.list_tips_to_verify():
        if not _match_could_be_over(tip, now.date()):
            summary["future"].append(tip["id"])
            continue
        summary["checked"] += 1
        try:
            res = check_tip_result(tip, usage)
        except Exception as exc:  # one bad tip shouldn't stop the batch
            summary["errors"].append({"tip_id": tip["id"], "match": tip["match_name"], "error": str(exc)})
            continue
        if res["status"] == "FINISHED":
            # Keep any result already settled (e.g. an admin override); fill only what's pending.
            result = res["outcome"] if tip["result"] == "PENDING" else tip["result"]
            alt = res["alt_outcome"] if tip["alt_result"] == "PENDING" else None
            final_score = tip["final_score"] or res["final_score"]
            db.update_tip_result(tip["id"], result, final_score, alt)
            summary["settled"].append({"tip_id": tip["id"], "match": tip["match_name"],
                                       "final_score": final_score, "market": tip["user_proposed_tip"],
                                       "result": result, "alt_market": tip["alt_market"] or "",
                                       "alt_result": (alt or tip["alt_result"]) if tip["alt_market"] else "",
                                       "notes": "; ".join(res["notes"])})
        else:
            summary["not_played"].append(tip["id"])


def auto_verify_on_startup():
    """Throttled check for app startup; never raises so it can't block the app."""
    try:
        return verify_pending_tips(force=False)
    except Exception as exc:
        return {"skipped": True, "reason": f"error: {exc}"}


if __name__ == "__main__":
    import json
    import sys
    db.init_db()
    print(json.dumps(verify_pending_tips(force="--force" in sys.argv), indent=2))
