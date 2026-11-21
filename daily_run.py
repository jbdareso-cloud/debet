"""Scheduled daily run (GitHub Actions, 13:00 Uganda time): read today's 4 TaifaTips tips and analyse them.

Safe to run several times a day: it does nothing if today's run already happened or the tips aren't posted
yet (a later scheduled attempt picks them up). Needs DATABASE_URL and OPENROUTER_API_KEY in the environment.
Run by hand with:  python daily_run.py
"""

import os
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()  # local runs read .env; on GitHub the secrets are already in the environment

import analysis_jobs as ajobs  # noqa: E402
import database as db  # noqa: E402
import tipster_site as ts  # noqa: E402
from debate_engine import parse_tip  # noqa: E402

UGANDA = timezone(timedelta(hours=3))
DONE_KEY = "AUTO_RUN_DATE"   # settings key: the last day the automatic run analysed tips
TIPSTER_SLOTS = 4


def main():
    today = datetime.now(UGANDA).date().isoformat()
    if not db.using_postgres() and not os.environ.get("DEBET_DAILY_ALLOW_LOCAL"):  # the flag is for tests
        sys.exit("DATABASE_URL is not set to the Supabase database - stopping so nothing is saved locally.")
    db.init_db(seed_admin=False)
    if db.get_setting(DONE_KEY) == today:
        print(f"Already ran for {today}. Nothing to do.")
        return

    rows, _ = ts.read_tips()
    todays = [r for r in rows if r["date"] == today and not r["error"]][:TIPSTER_SLOTS]
    if not todays:
        print(f"TaifaTips hasn't posted tips for {today} yet. A later run will try again.")
        return

    already = db.find_duplicates([(parse_tip(r["line"])["match_name"], parse_tip(r["line"])["market"])
                                  for r in todays])
    entries = [{"line": r["line"], "group": db.TIP_GROUPS[0], "slot_number": n}
               for n, r in enumerate(todays, 1)
               if (parse_tip(r["line"])["match_name"].lower(), parse_tip(r["line"])["market"].lower()) not in already]
    db.update_setting(DONE_KEY, today)  # mark first, so an overlapping retry can't pay twice
    if not entries:
        print("Today's tips were already analysed (e.g. by hand). Nothing to do.")
        return

    print(f"Analysing {len(entries)} tip(s) for {today}:", *[e["line"] for e in entries], sep="\n  ")
    job_id = ajobs.run_now(entries, "", today)
    job = db.get_job(job_id)
    db.update_job(job_id, message=f"🤖 Automatic run · {job['message']}")
    print(f"Done: {job['status']} · {job['message']} · AI cost ${job['cost_usd']:.3f}")
    if job["status"] != "done":
        sys.exit(1)  # shows as a failed run on GitHub


if __name__ == "__main__":
    main()
