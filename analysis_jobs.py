"""Run an analysis batch in the background so it survives a locked phone, a closed tab or a refresh.

The page only starts the job and then reads its progress from the database. Each tip is saved the
moment its analysis finishes; the day's challenge round runs at the end and updates the winner.
"""

import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

import database as db
import value_engine as ve
from debate_engine import parse_tip

WORKERS = 2  # more parallel calls trigger OpenRouter "402 ... in-flight requests" errors


def start(entries, notes, default_date):
    """entries: [{"line", "group", "slot_number"}]. Returns the job id; work continues in a thread."""
    job_id = db.create_job(len(entries))
    threading.Thread(target=_run, args=(job_id, entries, notes, default_date), daemon=True,
                     name=f"debet-job-{job_id}").start()
    return job_id


def _save(entry, result, error, default_date):
    tip = parse_tip(entry["line"])
    db.delete_failed_duplicates(tip["match_name"], tip["market"])
    cost = ((result or {}).get("cost") or {}).get("total_usd") if result else None
    if result:
        fields = ve.tip_fields(result, entry["group"], entry["slot_number"], default_date)
    else:
        fields = {"match_name": tip["match_name"], "user_proposed_tip": tip["market"], "odds": tip["odds"],
                  "tip_group": entry["group"], "slot_number": entry["slot_number"], "match_date": default_date,
                  "full_debate_log": f"Analysis failed: {error}"}
    fields["ai_cost"] = cost
    saved = db.save_tip(**fields)
    db.record_ai_cost("Analysis", cost)
    return saved["id"], cost or 0


def _run(job_id, entries, notes, default_date):
    tip_ids, total_cost, results = [], 0.0, []  # results: (result_dict, tip_id)
    try:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = {pool.submit(ve.analyze_tip, e["line"], notes, default_date): e for e in entries}
            for done, fut in enumerate(as_completed(futures), 1):
                entry = futures[fut]
                result, error = None, None
                try:
                    result = fut.result()
                except Exception as exc:  # one bad tip must not stop the batch
                    error = str(exc)
                    cost = (getattr(exc, "debate_cost", None) or {}).get("total_usd")
                    db.record_ai_cost("Analysis (failed)", cost)
                    total_cost += cost or 0
                try:
                    tid, cost = _save(entry, result, error, default_date)
                    tip_ids.append(tid)
                    total_cost += cost
                    if result:
                        results.append((result, tid))
                except Exception as exc:
                    print(f"[DeBet job {job_id}] saving {entry['line']!r} failed: {exc}", flush=True)
                db.update_job(job_id, done=done, tip_ids=tip_ids, cost_usd=round(total_cost, 4),
                              message=f"Analysed {done} of {len(entries)}...")

        db.update_job(job_id, message="Checking the best candidate...")
        _, message, day_cost = ve.choose_best([r for r, _ in results])
        challenge_cost = (day_cost or {}).get("total_usd") or 0
        db.record_ai_cost("Challenge round", challenge_cost)
        total_cost += challenge_cost
        for result, tid in results:  # write back what the challenge round changed
            if result.get("challenge"):
                fields = ve.tip_fields(result, None, None)
                db.update_tip_fields(tid, **{k: fields[k] for k in (
                    "decision", "ai_verdict", "p_final", "value", "challenge_note", "full_debate_log", "risk_factor")
                    if k in fields})
        db.update_job(job_id, status="done", message=message, cost_usd=round(total_cost, 4))
    except Exception as exc:
        traceback.print_exc()
        db.update_job(job_id, status="failed", message=f"Analysis stopped: {exc}", tip_ids=tip_ids,
                      cost_usd=round(total_cost, 4))
