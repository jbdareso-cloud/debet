"""Settle finished tips from API-Football's free plan (api-football.com) - no AI, $0.

One request per match day returns every fixture with its final score. Team names are matched loosely
(accents, 'FC', 'AFC' and similar are ignored), then result_verifier.settle_market decides WON/LOST/VOID
from the 90-minute score. Needs API_FOOTBALL_KEY in the environment; without it nothing happens.
"""

import json
import os
import re
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher

import database as db
from result_verifier import settle_market

API_URL = "https://v3.football.api-sports.io/fixtures?date={day}&timezone=Africa/Kampala"
FINISHED = {"FT", "AET", "PEN"}          # finished statuses; AET/PEN are settled on the 90-minute score
VOID_STATUSES = {"PST", "CANC", "ABD", "AWD", "WO"}
LOOKBACK_DAYS = 3                         # settle pending tips from up to this many days back
UGANDA = timezone(timedelta(hours=3))
_NOISE = r"\b(fc|afc|cf|sc|ac|fk|sk|bk|if|ik|cd|ud|sd|sv|vfb|vfl|tsg|ssc|as|us|rc|real|club|de|the)\b"


def _norm(name):
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(_NOISE, " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _same_team(a, b):
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    return SequenceMatcher(None, a, b).ratio() >= 0.8


MIN_GAP_SECONDS = 7      # at most ~8 requests a minute - the free plan's per-minute limit is 10
MAX_PER_RUN = 40         # safety cap per job run (the free plan allows 100 a day)
_state = {"last": 0.0, "count": 0, "blocked": None}


def api_get(endpoint, key, **params):
    """One polite API-Football request: paced, capped per run, and stops for the whole run after a refusal
    (suspension, limit reached), so a problem never turns into a burst of further requests."""
    if _state["blocked"]:
        raise RuntimeError(_state["blocked"])
    if _state["count"] >= MAX_PER_RUN:
        raise RuntimeError(f"API-Football: per-run cap of {MAX_PER_RUN} requests reached")
    wait = MIN_GAP_SECONDS - (time.monotonic() - _state["last"])
    if wait > 0:
        time.sleep(wait)
    _state["last"], _state["count"] = time.monotonic(), _state["count"] + 1
    url = "https://v3.football.api-sports.io/" + endpoint + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(urllib.request.Request(url, headers={"x-apisports-key": key}), timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    errors = data.get("errors")
    if errors:
        text = f"API-Football: {errors}"
        if any(w in str(errors).lower() for w in ("suspend", "limit", "token", "access", "requests")):
            _state["blocked"] = text  # don't keep knocking on a closed door this run...
            try:  # ...or in the next runs: pause API-Football for a day
                db.update_setting(AF_PAUSE_KEY, datetime.now(timezone.utc).isoformat())
            except Exception:
                pass
        raise RuntimeError(text)
    return data.get("response") or []


AF_PAUSE_KEY = "API_FOOTBALL_PAUSED_AT"


def af_available():
    """False for 24 hours after API-Football refused us (suspension / limit), so no run keeps calling it."""
    try:
        at = db.get_setting(AF_PAUSE_KEY)
        return not at or datetime.now(timezone.utc) - datetime.fromisoformat(at) > timedelta(hours=24)
    except Exception:
        return True


def fetch_day(day, key):
    """All fixtures on `day` (a date): [{"home", "away", "status", "score" ('2-1' at 90 min or None)}]."""
    games = []
    for f in api_get("fixtures", key, date=day.isoformat(), timezone="Africa/Kampala"):
        status = ((f.get("fixture") or {}).get("status") or {}).get("short")
        full = (f.get("score") or {}).get("fulltime") or {}
        h, a = full.get("home"), full.get("away")
        if h is None or a is None:  # some feeds only fill "goals"
            goals = f.get("goals") or {}
            h, a = goals.get("home"), goals.get("away")
        teams, league = f.get("teams") or {}, f.get("league") or {}
        games.append({"home": (teams.get("home") or {}).get("name", ""),
                      "away": (teams.get("away") or {}).get("name", ""),
                      "status": status, "score": f"{h}-{a}" if status in FINISHED and h is not None else None,
                      # for the pre-match data (match_data.py)
                      "fixture_id": (f.get("fixture") or {}).get("id"), "kickoff": (f.get("fixture") or {}).get("date"),
                      "home_id": (teams.get("home") or {}).get("id"), "away_id": (teams.get("away") or {}).get("id"),
                      "league_id": league.get("id"), "season": league.get("season"),
                      "league": f"{league.get('country') or ''} {league.get('name') or ''}".strip()})
    return games


def find_game(match_name, games):
    home, _, away = (match_name or "").partition(" vs ")
    for g in games:
        homes = [g["home"]] + [n for n in g.get("home_alts") or [] if n]
        aways = [g["away"]] + [n for n in g.get("away_alts") or [] if n]
        if any(_same_team(home, h) for h in homes) and any(_same_team(away, a) for a in aways):
            return g
    return None


# --- football-data.org (free plan: the big leagues + Champions League) ----------------------------------
FD_URL = "https://api.football-data.org/v4/matches"
FD_VOID = {"POSTPONED", "CANCELLED", "SUSPENDED", "AWARDED"}


def fd_fetch_day(day, key):
    """Same shape as fetch_day, from football-data.org. One request covers day-1..day+1 (time zones)."""
    params = urllib.parse.urlencode({"dateFrom": (day - timedelta(days=1)).isoformat(),
                                     "dateTo": (day + timedelta(days=1)).isoformat()})
    req = urllib.request.Request(f"{FD_URL}?{params}", headers={"X-Auth-Token": key})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    games = []
    for m in data.get("matches") or []:
        score = m.get("score") or {}
        status = m.get("status")
        regular = score.get("regularTime") if score.get("duration") not in (None, "REGULAR") else None
        ft = regular or score.get("fullTime") or {}
        h, a = ft.get("home"), ft.get("away")
        teams = {s: m.get(f"{s}Team") or {} for s in ("home", "away")}
        games.append({"home": teams["home"].get("name") or "", "away": teams["away"].get("name") or "",
                      "home_alts": [teams["home"].get("shortName"), teams["home"].get("tla")],
                      "away_alts": [teams["away"].get("shortName"), teams["away"].get("tla")],
                      "status": "PST" if status in FD_VOID else status,
                      "score": f"{h}-{a}" if status == "FINISHED" and h is not None and a is not None else None})
    return games


# --- AI fallback (web search, ~$0.005 a match) for leagues the free sources don't cover -----------------
AI_TRIES_KEY = "AI_RESULT_TRIES"   # settings: {tip_id: attempts}, so no match is ever paid for more than twice
AI_MAX_TRIES = 2


def _ai_fallback(tips, log):
    from result_verifier import check_tip_result
    from debate_engine import summarize_usage
    tries = json.loads(db.get_setting(AI_TRIES_KEY, "{}"))
    settled, usage = 0, []
    for t in tips:
        n = tries.get(str(t["id"]), 0)
        if n >= AI_MAX_TRIES:
            continue
        tries[str(t["id"])] = n + 1
        try:
            res = check_tip_result(t, usage)
        except Exception as exc:
            log(f"  {t['match_name']}: AI check failed ({str(exc)[:80]}).")
            continue
        if res.get("status") == "FINISHED" and res.get("outcome"):
            db.update_tip_result(t["id"], res["outcome"], res["final_score"])
            settled += 1
            log(f"  {t['match_name']} {res['final_score']}: {t['user_proposed_tip']} -> {res['outcome']} (AI check)")
        else:
            log(f"  {t['match_name']}: AI check found no final score (try {n + 1} of {AI_MAX_TRIES}).")
    db.update_setting(AI_TRIES_KEY, json.dumps(tries))
    cost = (summarize_usage(usage) or {}).get("total_usd")
    db.record_ai_cost("Result check (AI)", cost)
    if cost:
        log(f"  AI result checks cost ${cost:.4f}")
    return settled


def settle_pending(key=None, today=None, log=print, include_today=False, lookback=LOOKBACK_DAYS, allow_ai=False):
    """Settle pending tips from the last `lookback` days (Uganda time); with include_today also today's
    matches that have already finished (only full-time 90-minute scores are used).
    Sources in order: API-Football (if its key works), football-data.org (free, big leagues), then - only
    when allow_ai and only for earlier days' matches neither source found - the AI web check (max 2 tries
    per match). Returns the number settled."""
    today = today or datetime.now(UGANDA).date()
    sources = []
    af_key = (key or os.environ.get("API_FOOTBALL_KEY", "")).strip()
    fd_key = os.environ.get("FOOTBALL_DATA_KEY", "").strip()
    if af_key and af_available():
        sources.append(("API-Football", lambda d: fetch_day(d, af_key)))
    elif af_key:
        log("API-Football is paused for 24 hours after a refusal (suspended or over its limit).")
    if fd_key:
        sources.append(("football-data.org", lambda d: fd_fetch_day(d, fd_key)))
    if allow_ai:  # free CSV files (many more leagues, 1-3 days behind) - tried just before the paid AI check
        import couk_data
        sources.append(("football-data.co.uk", lambda d: couk_data.fetch_day(d) if d < today else []))
    oldest = today - timedelta(days=lookback)
    by_day = {}
    for t in db.list_tips(result="PENDING", limit=200):
        try:
            day = date.fromisoformat(str(t.get("match_date"))[:10])
        except ValueError:
            continue
        if oldest <= day < today or (include_today and day == today):
            by_day.setdefault(day, []).append(t)
    settled, for_ai = 0, []
    for day, tips in sorted(by_day.items()):
        remaining = tips
        for name, fetch in sources:
            if not remaining:
                break
            try:
                games = fetch(day)
            except Exception as exc:
                log(f"Results for {day} from {name}: couldn't fetch ({str(exc)[:120]}).")
                continue
            not_found = []
            for t in remaining:
                g = find_game(t["match_name"], games)
                if not g:
                    not_found.append(t)
                    continue
                if g["status"] in VOID_STATUSES:
                    log(f"  {t['match_name']}: {g['status']} (postponed/cancelled) - left pending for you to decide.")
                    continue
                if not g["score"]:
                    log(f"  {t['match_name']}: not finished yet ({g['status']}).")
                    continue
                home, _, away = t["match_name"].partition(" vs ")
                outcome = settle_market(t["user_proposed_tip"], g["score"], home, away)
                if not outcome:
                    log(f"  {t['match_name']} {g['score']}: market '{t['user_proposed_tip']}' needs a manual result.")
                    continue
                db.update_tip_result(t["id"], outcome, g["score"])
                settled += 1
                log(f"  {t['match_name']} {g['score']}: {t['user_proposed_tip']} -> {outcome} ({name})")
            remaining = not_found
        for t in remaining:
            log(f"  {t['match_name']}: not found in the free sources.")
        if day < today:
            for_ai += remaining
    if allow_ai and for_ai:
        settled += _ai_fallback(for_ai, log)
    log(f"Results: settled {settled} tip(s).")
    return settled


def check(day=None):
    """Read-only test of the key: fetch one day and show which of that day's tips are found (changes nothing)."""
    key = os.environ.get("API_FOOTBALL_KEY", "").strip()
    if not key:
        raise SystemExit("API_FOOTBALL_KEY is not set.")
    day = day or (datetime.now(UGANDA).date() - timedelta(days=1))
    games = fetch_day(day, key)
    finished = [g for g in games if g["score"]]
    print(f"Key works: {len(games)} fixtures on {day}, {len(finished)} finished.")
    tips = [t for t in db.list_tips(limit=200) if str(t.get("match_date"))[:10] == day.isoformat()]
    for t in tips:
        g = find_game(t["match_name"], games)
        print(f"  {t['match_name']}: " + (f"found as {g['home']} vs {g['away']}, {g['status']} {g['score'] or ''}"
                                          if g else "NOT found"))


if __name__ == "__main__":  # python results_api.py --check [YYYY-MM-DD]
    import sys
    from dotenv import load_dotenv
    load_dotenv()
    if "--settle" in sys.argv:  # the hourly evening job: today's finished matches and yesterday's leftovers
        if not db.using_postgres():
            raise SystemExit("DATABASE_URL is not set to the Supabase database.")
        # the AI fallback only at the 02:xx run (after the night's matches) - the 10:00 daily run is the other
        settle_pending(include_today=True, lookback=1, allow_ai=datetime.now(UGANDA).hour == 2)
    elif "--check" in sys.argv:
        extra = [a for a in sys.argv[1:] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", a)]
        check(date.fromisoformat(extra[0]) if extra else None)
