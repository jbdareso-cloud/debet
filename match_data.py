"""Verified pre-match statistics from API-Football's free plan, for Grok and Claude to analyse. No AI, $0.

Per match: the day's fixture list (1 request, shared by every tip that day), the fixture's statistics
(/predictions: season form, home/away records, goals, last 5, head-to-head), injuries/suspensions
(/injuries) and the league table (/standings, shared per league). About 3 requests per tip.
The bookmaker-style 'advice' and percentages API-Football adds are deliberately left out, so the models
judge the numbers themselves. Needs API_FOOTBALL_KEY; without it (or if the match isn't found) returns "".
"""

import os
from datetime import date

import results_api as ra

BASE = "https://v3.football.api-sports.io/"
_cache = {}  # (endpoint, params) -> response list, for one process (one run)


def _get(endpoint, key, **params):
    ck = (endpoint, tuple(sorted(params.items())))
    if ck not in _cache:
        _cache[ck] = ra.api_get(endpoint, key, **params)
    return _cache[ck]


def _day_games(day, key):
    ck = ("fixtures-day", day.isoformat())
    if ck not in _cache:
        _cache[ck] = ra.fetch_day(day, key)
    return _cache[ck]


def _record(fx, where):
    """'W5 D3 L4' from a fixtures block for 'home' / 'away' / 'total'."""
    g = lambda k: ((fx.get(k) or {}).get(where))
    w, d, l = g("wins"), g("draws"), g("loses")
    return f"W{w} D{d} L{l}" if None not in (w, d, l) else "?"


def _goals(league, where):
    f = (((league.get("goals") or {}).get("for") or {}).get("total") or {}).get(where)
    a = (((league.get("goals") or {}).get("against") or {}).get("total") or {}).get(where)
    return f"{f}-{a}" if f is not None and a is not None else "?"


def _team_lines(name, side, t):
    lg = t.get("league") or {}
    fx = lg.get("fixtures") or {}
    form = (lg.get("form") or "")[-5:]
    l5 = t.get("last_5") or {}
    l5g = l5.get("goals") or {}
    avg_for = ((l5g.get("for") or {}).get("average"))
    avg_against = ((l5g.get("against") or {}).get("average"))
    cs = (lg.get("clean_sheet") or {}).get("total")
    fts = (lg.get("failed_to_score") or {}).get("total")
    played = (fx.get("played") or {}).get("total")
    return (f"{name}: season {_record(fx, 'total')} in {played} games, goals {_goals(lg, 'total')}; "
            f"{side} {_record(fx, side)}, {side} goals {_goals(lg, side)}; last 5 results {form or '?'}; "
            f"last 5 avg goals for/against {avg_for or '?'}/{avg_against or '?'}; "
            f"clean sheets {cs if cs is not None else '?'}, failed to score {fts if fts is not None else '?'}")


def _table(key, league_id, season, team_ids):
    out = {}
    try:
        resp = _get("standings", key, league=league_id, season=season)
    except Exception:
        return out
    for group in ((resp[0].get("league") or {}).get("standings") or []) if resp else []:
        for row in group:
            tid = (row.get("team") or {}).get("id")
            if tid in team_ids:
                out[tid] = f"{row.get('rank')}{_suffix(row.get('rank'))} ({row.get('points')} pts, " \
                           f"{(row.get('all') or {}).get('played')} played)"
    return out


def _suffix(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    return "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def match_block(match_name, day, key=None):
    """A 'MATCH DATA' text block for one fixture, or '' if there's no key or the match can't be found."""
    key = key or os.environ.get("API_FOOTBALL_KEY", "").strip()
    if not key or not match_name:
        return ""
    if not ra.af_available():
        return "MATCH DATA: unavailable (API-Football is paused after refusing requests) - rely on the brief.\n"
    try:
        day = date.fromisoformat(str(day)[:10])
        game = ra.find_game(match_name, _day_games(day, key))
        if not game or not game.get("fixture_id"):
            return f"MATCH DATA: {match_name} was not found in the fixture list for {day} - rely on the brief.\n"
        pred = (_get("predictions", key, fixture=game["fixture_id"]) or [{}])[0]
        teams = pred.get("teams") or {}
        home, away = game["home"], game["away"]
        lines = [f"MATCH DATA (verified statistics from API-Football; {game['league']}):"]
        table = _table(key, game["league_id"], game["season"], {game["home_id"], game["away_id"]})
        if table:
            lines.append(f"League table: {home} {table.get(game['home_id'], '?')} vs "
                         f"{away} {table.get(game['away_id'], '?')}")
        if teams:
            lines.append(_team_lines(home, "home", teams.get("home") or {}))
            lines.append(_team_lines(away, "away", teams.get("away") or {}))
        h2h = pred.get("h2h") or []
        if h2h:
            recent = sorted(h2h, key=lambda f: (f.get("fixture") or {}).get("date") or "", reverse=True)[:5]
            lines.append("Head-to-head (latest first): " + "; ".join(
                f"{((f.get('fixture') or {}).get('date') or '')[:10]} "
                f"{((f.get('teams') or {}).get('home') or {}).get('name')} {(f.get('goals') or {}).get('home')}-"
                f"{(f.get('goals') or {}).get('away')} {((f.get('teams') or {}).get('away') or {}).get('name')}"
                for f in recent))
        try:
            inj = _get("injuries", key, fixture=game["fixture_id"])
        except Exception:
            inj = None
        if inj is not None:
            by_team = {}
            for row in inj:
                p = row.get("player") or {}
                by_team.setdefault((row.get("team") or {}).get("name", "?"), []).append(
                    f"{p.get('name')} ({p.get('reason') or p.get('type') or 'out'})")
            lines.append("Injuries/suspensions: " + ("; ".join(f"{t}: {', '.join(ps[:8])}" for t, ps in by_team.items())
                                                       if by_team else "none listed"))
        return "\n".join(lines) + "\n"
    except Exception as exc:  # data is a bonus - never stop the analysis
        return f"MATCH DATA: unavailable ({str(exc)[:80]}) - rely on the brief.\n"


if __name__ == "__main__":  # python match_data.py 2026-11-22 "Osasuna vs Malaga"
    import sys
    from dotenv import load_dotenv
    load_dotenv()
    print(match_block(sys.argv[2], sys.argv[1]) or "No API_FOOTBALL_KEY set.")
