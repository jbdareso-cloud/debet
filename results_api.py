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
            _state["blocked"] = text  # don't keep knocking on a closed door this run
        raise RuntimeError(text)
    return data.get("response") or []


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
        if _same_team(home, g["home"]) and _same_team(away, g["away"]):
            return g
    return None


def settle_pending(key=None, today=None, log=print, include_today=False, lookback=LOOKBACK_DAYS):
    """Settle pending tips from the last `lookback` days (Uganda time); with include_today also today's
    matches that have already finished (only full-time scores are ever used). Returns the number settled."""
    key = key or os.environ.get("API_FOOTBALL_KEY", "").strip()
    if not key:
        log("Results: no API_FOOTBALL_KEY set - skipping.")
        return 0
    today = today or datetime.now(UGANDA).date()
    oldest = today - timedelta(days=lookback)
    by_day = {}
    for t in db.list_tips(result="PENDING", limit=200):
        try:
            day = date.fromisoformat(str(t.get("match_date"))[:10])
        except ValueError:
            continue
        if oldest <= day < today or (include_today and day == today):
            by_day.setdefault(day, []).append(t)
    settled = 0
    for day, tips in sorted(by_day.items()):
        try:
            games = fetch_day(day, key)
        except Exception as exc:
            log(f"Results for {day}: couldn't fetch ({exc}).")
            continue
        for t in tips:
            g = find_game(t["match_name"], games)
            if not g:
                log(f"  {t['match_name']}: not found - enter it by hand if it stays pending.")
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
            log(f"  {t['match_name']} {g['score']}: {t['user_proposed_tip']} -> {outcome}")
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
        settle_pending(include_today=True, lookback=1)
    elif "--check" in sys.argv:
        extra = [a for a in sys.argv[1:] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", a)]
        check(date.fromisoformat(extra[0]) if extra else None)
