"""Team statistics and results from football-data.co.uk's free CSV files - no AI, $0.

Main leagues (England 5 divisions, Scotland, Germany, Italy, Spain, France top 2, Netherlands, Belgium,
Portugal, Turkey, Greece) have one file per season with shots, shots on target, corners and cards.
'Extra' leagues (Denmark, Poland, Norway, Sweden, Switzerland, Austria, ...) have one file with all seasons
and results only. Files are downloaded politely (1 second apart) and cached for the run.
"""

import csv
import io
import time
import urllib.request
from datetime import date, datetime, timedelta

import results_api as ra

BASE = "https://www.football-data.co.uk/"
UA = "Mozilla/5.0 (compatible; DeBet personal reader)"
MAIN = {"E0": "England Premier League", "E1": "England Championship", "E2": "England League One",
        "E3": "England League Two", "EC": "England National League", "SC0": "Scotland Premiership",
        "SC1": "Scotland Championship", "SC2": "Scotland League One", "SC3": "Scotland League Two",
        "D1": "Germany Bundesliga", "D2": "Germany 2. Bundesliga", "I1": "Italy Serie A", "I2": "Italy Serie B",
        "SP1": "Spain La Liga", "SP2": "Spain Segunda", "F1": "France Ligue 1", "F2": "France Ligue 2",
        "N1": "Netherlands Eredivisie", "B1": "Belgium Pro League", "P1": "Portugal Primeira Liga",
        "T1": "Turkey Super Lig", "G1": "Greece Super League"}
EXTRA = {"DNK": "Denmark", "POL": "Poland", "NOR": "Norway", "SWE": "Sweden", "SWZ": "Switzerland",
         "AUT": "Austria", "ROU": "Romania", "RUS": "Russia", "FIN": "Finland", "IRL": "Ireland",
         "ARG": "Argentina", "BRA": "Brazil", "MEX": "Mexico", "USA": "USA", "JPN": "Japan", "CHN": "China"}
GAP_SECONDS = 1.0
_files, _last = {}, [0.0]


def _season(d):
    """'2627' for the season that contains date d (seasons start in July)."""
    y = d.year % 100 if d.month >= 7 else (d.year - 1) % 100
    return f"{y:02d}{(y + 1) % 100:02d}"


def _date(text):
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except (ValueError, AttributeError):
            pass
    return None


def _num(row, *cols):
    for c in cols:
        try:
            return int(float(row[c]))
        except (KeyError, TypeError, ValueError):
            continue
    return None


def _download(path):
    if path in _files:
        return _files[path]
    wait = GAP_SECONDS - (time.monotonic() - _last[0])
    if wait > 0:
        time.sleep(wait)
    _last[0] = time.monotonic()
    rows = []
    try:
        with urllib.request.urlopen(urllib.request.Request(BASE + path, headers={"User-Agent": UA}), timeout=30) as r:
            text = r.read().decode("utf-8-sig", "replace")
        for row in csv.DictReader(io.StringIO(text)):
            d = _date(row.get("Date", ""))
            home, away = row.get("HomeTeam") or row.get("Home"), row.get("AwayTeam") or row.get("Away")
            hg, ag = _num(row, "FTHG", "HG"), _num(row, "FTAG", "AG")
            if not (d and home and away) or hg is None or ag is None:
                continue
            rows.append({"date": d, "home": home.strip(), "away": away.strip(), "hg": hg, "ag": ag,
                         "season": (row.get("Season") or "").strip(),
                         "hs": _num(row, "HS"), "as": _num(row, "AS"), "hst": _num(row, "HST"), "ast": _num(row, "AST")})
    except Exception:
        rows = []
    _files[path] = rows
    return rows


def _league_rows(code, d):
    """This season's finished games for a league code, plus older seasons for head-to-head."""
    if code in MAIN:
        season = _download(f"mmz4281/{_season(d)}/{code}.csv")
        return season, season
    every = _download(f"new/{code}.csv")
    latest = max((r["season"] for r in every), default="")
    return [r for r in every if r["season"] == latest], every


def _find_league(home, away, d):
    """(code, league name, this season's rows, all rows) for the league where both teams play, or None."""
    for code in list(MAIN) + list(EXTRA):
        season, every = _league_rows(code, d)
        teams = {r["home"] for r in season} | {r["away"] for r in season}
        h = next((t for t in teams if ra._same_team(home, t)), None)
        a = next((t for t in teams if ra._same_team(away, t)), None)
        if h and a:
            return code, MAIN.get(code) or EXTRA[code], h, a, season, every
    return None


def _record(games, team):
    w = sum(1 for g in games if (g["home"] == team and g["hg"] > g["ag"]) or (g["away"] == team and g["ag"] > g["hg"]))
    dr = sum(1 for g in games if g["hg"] == g["ag"])
    l = len(games) - w - dr
    gf = sum(g["hg"] if g["home"] == team else g["ag"] for g in games)
    ga = sum(g["ag"] if g["home"] == team else g["hg"] for g in games)
    return w, dr, l, gf, ga


def _table(season):
    pts = {}
    for g in season:
        for team, f, a in ((g["home"], g["hg"], g["ag"]), (g["away"], g["ag"], g["hg"])):
            p = pts.setdefault(team, [0, 0, 0])
            p[0] += 3 if f > a else 1 if f == a else 0
            p[1] += f - a
            p[2] += 1
    order = sorted(pts, key=lambda t: (-pts[t][0], -pts[t][1]))
    return {t: (i + 1, pts[t][0], pts[t][2], len(order)) for i, t in enumerate(order)}


def _team_line(team, side, season, table, before):
    games = sorted((g for g in season if team in (g["home"], g["away"]) and g["date"] < before), key=lambda g: g["date"])
    if not games:
        return f"{team}: no games found this season"
    w, d, l, gf, ga = _record(games, team)
    venue = [g for g in games if g[side] == team]
    vw, vd, vl, vgf, vga = _record(venue, team)
    last6 = []
    for g in reversed(games[-6:]):
        f, a = (g["hg"], g["ag"]) if g["home"] == team else (g["ag"], g["hg"])
        last6.append(("W" if f > a else "D" if f == a else "L") + f"{f}-{a}")
    pos = table.get(team)
    pos_txt = f"{pos[0]}/{pos[3]} ({pos[1]} pts)" if pos else "?"
    line = (f"{team}: {pos_txt}; season W{w} D{d} L{l}, goals {gf}-{ga} ({gf / len(games):.1f}/{ga / len(games):.1f} "
            f"per game); {side} W{vw} D{vd} L{vl}, goals {vgf}-{vga}; last 6 (latest first) {' '.join(last6)}")
    shots = [(g["hs"], g["hst"], g["as"], g["ast"]) if g["home"] == team else (g["as"], g["ast"], g["hs"], g["hst"])
             for g in games[-6:] if None not in (g["hs"], g["hst"], g["as"], g["ast"])]
    if shots:
        n = len(shots)
        line += (f"; last {n} shots for/against {sum(s[0] for s in shots) / n:.1f}/{sum(s[2] for s in shots) / n:.1f}, "
                 f"on target {sum(s[1] for s in shots) / n:.1f}/{sum(s[3] for s in shots) / n:.1f} per game")
    return line


def stats_block(match_name, day):
    """A 'TEAM STATS' text block from the CSV files, or '' if the league isn't covered."""
    try:
        d = date.fromisoformat(str(day)[:10])
        home, _, away = (match_name or "").partition(" vs ")
        found = _find_league(home, away, d)
        if not found:
            return ""
        code, league, h, a, season, every = found
        if code in MAIN:  # older seasons for head-to-head (only for the league actually needed)
            for back in (1, 2):
                every = every + _download(f"mmz4281/{_season(d - timedelta(days=365 * back))}/{code}.csv")
        table = _table([g for g in season if g["date"] < d])
        latest = max((g["date"] for g in season), default=None)
        lines = [f"TEAM STATS (results from football-data.co.uk, {league}, up to {latest}):",
                 _team_line(h, "home", season, table, d), _team_line(a, "away", season, table, d)]
        h2h = sorted((g for g in every if {g["home"], g["away"]} == {h, a} and g["date"] < d),
                     key=lambda g: g["date"], reverse=True)[:5]
        if h2h:
            lines.append("Head-to-head (latest first): " + "; ".join(
                f"{g['date']} {g['home']} {g['hg']}-{g['ag']} {g['away']}" for g in h2h))
        return "\n".join(lines) + "\n"
    except Exception as exc:  # stats are a bonus - never stop the analysis
        return f"TEAM STATS: unavailable ({str(exc)[:80]}).\n"


def fetch_day(day):
    """Finished games on `day` from every covered league, in results_api's game format (free results)."""
    games = []
    for code in list(MAIN) + list(EXTRA):
        season, _ = _league_rows(code, day)
        for g in season:
            if g["date"] == day:
                games.append({"home": g["home"], "away": g["away"], "status": "FT", "score": f"{g['hg']}-{g['ag']}"})
    return games


if __name__ == "__main__":  # python couk_data.py 2026-11-23 "Brondby vs Midtjylland"
    import sys
    print(stats_block(sys.argv[2], sys.argv[1]) or "League not covered.")
