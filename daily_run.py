"""Scheduled daily run (GitHub Actions, hourly 10:00-18:00 Uganda time): read today's tips and analyse them.

Source: TaifaTips (4 tips -> slots #1-#4). More sites can be added to SOURCES if they allow it.
Before that, earlier days' pending tips are settled from API-Football's free results (results_api.py).
Safe to run several times a day: each source is analysed at most once per day, and a source that hasn't
posted yet is picked up by a later scheduled attempt. Tips already analysed by hand are skipped.
Needs DATABASE_URL and OPENROUTER_API_KEY in the environment. Run by hand with:  python daily_run.py
"""

import os
import re
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()  # local runs read .env; on GitHub the secrets are already in the environment

import analysis_jobs as ajobs  # noqa: E402
import database as db  # noqa: E402
import results_api  # noqa: E402
import tipster_site as ts  # noqa: E402
import value_engine as ve  # noqa: E402
from debate_engine import parse_tip  # noqa: E402

UGANDA = timezone(timedelta(hours=3))
# (name, reader, settings key marking the last day it was analysed, tip group, slots)
# Only sites that allow it and don't block GitHub's servers. (BetMines refused with HTTP 403, so slots
# #5-#7 are filled by hand.)
SOURCES = [
    ("TaifaTips", lambda today: ts.read_tips(), "AUTO_RUN_DATE", 0, [1, 2, 3, 4]),
]


def _odds(line):
    m = re.search(r"@ (\d+(?:\.\d+)?)$", line)
    return float(m.group(1)) if m else None


def _key(line):
    p = parse_tip(line)
    return p["match_name"].lower(), p["market"].lower()


def collect(today):
    """Today's not-yet-analysed tips from every source that isn't complete for today.
    Sites often post some tips at midday and the rest in the evening (for late kickoffs), so a source is
    only 'complete' once all its slots are filled; until then every hourly run picks up what's new.
    Returns (entries, sources_completed)."""
    entries, completed = [], []
    full = f"{today.isoformat()}:full"
    for name, reader, done_key, group, slots in SOURCES:
        if db.get_setting(done_key) == full:
            print(f"{name}: all {len(slots)} tips already analysed today.")
            continue
        try:
            rows, _ = reader(today)
        except RuntimeError as exc:
            print(f"{name}: {exc}")
            continue
        todays = [r for r in rows if r["date"] == today.isoformat() and not r["error"]]
        todays.sort(key=lambda r: (_odds(r["line"]) or 99) < ve.MIN_ODDS)  # too-short prices go last
        todays = todays[:len(slots)]
        if not todays:
            print(f"{name}: no tips for {today} yet - the next hourly run will look again.")
            continue
        already = db.find_duplicates([_key(r["line"]) for r in todays])
        new = [{"line": r["line"], "group": db.TIP_GROUPS[group], "slot_number": n}
               for n, r in zip(slots, todays) if _key(r["line"]) not in already]
        print(f"{name}: {len(todays)} of {len(slots)} tips posted today, {len(new)} not analysed yet.")
        if len(todays) >= len(slots):
            completed.append((name, done_key))
        entries += new
    return entries, completed


def main():
    today = datetime.now(UGANDA).date()
    if not db.using_postgres() and not os.environ.get("DEBET_DAILY_ALLOW_LOCAL"):  # the flag is for tests
        sys.exit("DATABASE_URL is not set to the Supabase database - stopping so nothing is saved locally.")
    db.init_db(seed_admin=False)

    try:  # first settle earlier days' tips from the free results service (no AI)
        # the 10:00 run may use the AI web check for earlier matches the free sources didn't find (max 2 tries)
        results_api.settle_pending(today=today, allow_ai=datetime.now(UGANDA).hour == 10)
    except Exception as exc:  # results must never stop today's analysis
        print(f"Results step failed: {exc}")

    entries, completed = collect(today)
    for _, done_key in completed:  # all slots posted: later runs today can skip the page
        db.update_setting(done_key, f"{today.isoformat()}:full")
    if not entries:
        print("Nothing new to analyse.")
        return

    print(f"Analysing {len(entries)} tip(s) for {today}:", *[e["line"] for e in entries], sep="\n  ")
    job_id = ajobs.run_now(entries, "", today.isoformat())
    job = db.get_job(job_id)
    print(f"Done: {job['status']} · {job['message']} · AI cost ${job['cost_usd']:.3f}")
    if job["status"] != "done":
        sys.exit(1)  # shows as a failed run on GitHub


if __name__ == "__main__":
    main()
