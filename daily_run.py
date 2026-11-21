"""Scheduled daily run (GitHub Actions, 13:00 Uganda time): read today's tips and analyse them.

Sources: TaifaTips (4 tips -> slots #1-#4) and BetMines Daily Bets (up to 3 tips -> slots #5-#7).
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
import tipster_site as ts  # noqa: E402
import value_engine as ve  # noqa: E402
from debate_engine import parse_tip  # noqa: E402

UGANDA = timezone(timedelta(hours=3))
# (name, reader, settings key marking the last day it was analysed, tip group, slots)
SOURCES = [
    ("TaifaTips", lambda today: ts.read_tips(), "AUTO_RUN_DATE", 0, [1, 2, 3, 4]),
    ("BetMines", lambda today: ts.read_betmines(today), "AUTO_RUN_BETMINES", 1, [5, 6, 7]),
]


def _odds(line):
    m = re.search(r"@ (\d+(?:\.\d+)?)$", line)
    return float(m.group(1)) if m else None


def _key(line):
    p = parse_tip(line)
    return p["match_name"].lower(), p["market"].lower()


def collect(today):
    """Today's new tips from every source not yet done today. Returns (entries, sources_found)."""
    entries, found = [], []
    for name, reader, done_key, group, slots in SOURCES:
        if db.get_setting(done_key) == today.isoformat():
            print(f"{name}: already done today.")
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
            print(f"{name}: no tips for {today} yet - a later run will try again.")
            continue
        found.append((name, done_key))
        already = db.find_duplicates([_key(r["line"]) for r in todays])
        new = [{"line": r["line"], "group": db.TIP_GROUPS[group], "slot_number": n}
               for n, r in zip(slots, todays) if _key(r["line"]) not in already]
        print(f"{name}: {len(todays)} tip(s) today, {len(new)} not analysed yet.")
        entries += new
    return entries, found


def main():
    today = datetime.now(UGANDA).date()
    if not db.using_postgres() and not os.environ.get("DEBET_DAILY_ALLOW_LOCAL"):  # the flag is for tests
        sys.exit("DATABASE_URL is not set to the Supabase database - stopping so nothing is saved locally.")
    db.init_db(seed_admin=False)

    entries, found = collect(today)
    for _, done_key in found:  # mark first, so an overlapping retry can't pay twice
        db.update_setting(done_key, today.isoformat())
    if not entries:
        print("Nothing new to analyse.")
        return

    print(f"Analysing {len(entries)} tip(s) for {today}:", *[e["line"] for e in entries], sep="\n  ")
    job_id = ajobs.run_now(entries, "", today.isoformat())
    job = db.get_job(job_id)
    db.update_job(job_id, message=f"🤖 Automatic run · {job['message']}")
    print(f"Done: {job['status']} · {job['message']} · AI cost ${job['cost_usd']:.3f}")
    if job["status"] != "done":
        sys.exit(1)  # shows as a failed run on GitHub


if __name__ == "__main__":
    main()
