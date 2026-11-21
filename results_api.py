"""Settle finished tips from API-Football's free plan (api-football.com) - no AI, $0.

One request per match day returns every fixture with its final score. Team names are matched loosely
(accents, 'FC', 'AFC' and similar are ignored), then result_verifier.settle_market decides WON/LOST/VOID
from the 90-minute score. Needs API_FOOTBALL_KEY in the environment; without it nothing happens.
"""

import json
import os
import re
import unicodedata
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


def fetch_day(day, key):
    """All fixtures on `day` (a date): [{"home", "away", "status", "score" ('2-1' at 90 min or None)}]."""
    req = urllib.request.Request(API_URL.format(day=day.isoformat()), headers={"x-apisports-key": key})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    errors = data.get("errors")
    if errors:
        raise RuntimeError(f"API-Football: {errors}")
    games = []
    for f in data.get("response") or []:
        status = ((f.get("fixture") or {}).get("status") or {}).get("short")
        full = (f.get("score") or {}).get("fulltime") or {}
        h, a = full.get("home"), full.get("away")
        if h is None or a is None:  # some feeds only fill "goals"
            goals = f.get("goals") or {}
            h, a = goals.get("home"), goals.get("away")
        games.append({"home": ((f.get("teams") or {}).get("home") or {}).get("name", ""),
                      "away": ((f.get("teams") or {}).get("away") or {}).get("name", ""),
                      "status": status, "score": f"{h}-{a}" if status in FINISHED and h is not None else None})
    return games


def find_game(match_name, games):
    home, _, away = (match_name or "").partition(" vs ")
    for g in games:
        if _same_team(home, g["home"]) and _same_team(away, g["away"]):
            return g
    return None


def settle_pending(key=None, today=None, log=print):
    """Settle pending tips whose match day is before today (Uganda time). Returns the number settled."""
    key = key or os.environ.get("API_FOOTBALL_KEY", "").strip()
    if not key:
        log("Results: no API_FOOTBALL_KEY set - skipping.")
        return 0
    today = today or datetime.now(UGANDA).date()
    oldest = today - timedelta(days=LOOKBACK_DAYS)
    by_day = {}
    for t in db.list_tips(result="PENDING", limit=200):
        try:
            day = date.fromisoformat(str(t.get("match_date"))[:10])
        except ValueError:
            continue
        if oldest <= day < today:
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
