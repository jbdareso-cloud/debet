"""Read tips from a screenshot of a tipster's table (Fixtures | Tips | Odd) using Claude's vision.

One call per batch of images (~1 cent). Returns tip lines in the format parse_tip() understands,
e.g. 'France vs Italy - 1 @ 1.51', grouped by the date shown in the table.
"""

import base64
import re
from datetime import date

from debate_engine import CLAUDE_FALLBACK_MODEL, CLAUDE_MODEL, _chat, _parse_json_object, parse_tip, summarize_usage

READ_MAX_TOKENS = 1200
MAX_IMAGES = 4

READ_SYSTEM = (
    "You read football betting-tip tables from screenshots and transcribe them exactly. "
    "Never invent or guess a match, tip or price. Reply with JSON only."
)

READ_USER = """Transcribe every tip row in these screenshot(s).
Each table has date header rows like "Friday 02.10.26" (day.month.two-digit year -> 20YY), then rows:
Fixture ("Home vs Away") | Tip | Odd | status icon. Ignore the status icons (ticks/circles) and any flags.
Tips look like: 1 (home win), 2 (away win), X (draw), 1X, X2, 12, Over 2.5, Under 2.5, BTS Yes, BTS No.
If the same row appears in more than one screenshot, include it once.

Return exactly:
{{"tips": [{{"date": "YYYY-MM-DD or null", "home": "...", "away": "...", "tip": "...", "odds": 1.51}}],
 "unreadable": ["<anything you could not read clearly>"]}}
Today is {today}."""


def _data_url(raw, mime):
    mime = mime if mime in ("image/png", "image/jpeg", "image/webp", "image/gif") else "image/png"
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


def _clean_tip(tip):
    """Turn table shorthand into a market parse_tip understands ('BTS Yes' -> 'BTTS Yes')."""
    t = re.sub(r"\s+", " ", str(tip or "")).strip()
    t = re.sub(r"^bts\b", "BTTS", t, flags=re.IGNORECASE)
    return t


def read_tips(images, today=None):
    """images: [(bytes, mime_type)]. Returns (rows, unreadable, cost_summary).

    rows: [{"date": 'YYYY-MM-DD' or None, "line": 'Home vs Away - Tip @ 1.51', "error": None or str}]
    """
    if not images:
        raise ValueError("Add at least one screenshot.")
    images = images[:MAX_IMAGES]
    usage = []
    content = [{"type": "text", "text": READ_USER.format(today=(today or date.today()).isoformat())}]
    content += [{"type": "image_url", "image_url": {"url": _data_url(raw, mime)}} for raw, mime in images]
    text, _ = _chat([CLAUDE_MODEL, CLAUDE_FALLBACK_MODEL],
                    [{"role": "system", "content": READ_SYSTEM}, {"role": "user", "content": content}],
                    temperature=0.0, max_tokens=READ_MAX_TOKENS, usage=usage, label="Read screenshot (Claude)")
    data = _parse_json_object(text)
    if not data or not isinstance(data.get("tips"), list):
        raise RuntimeError(f"Couldn't read tips from the screenshot (reply started: {text[:150]!r}).")

    rows, seen = [], set()
    for t in data["tips"]:
        if not isinstance(t, dict):
            continue
        home, away = str(t.get("home") or "").strip(), str(t.get("away") or "").strip()
        tip = _clean_tip(t.get("tip"))
        try:
            odds = float(str(t.get("odds")).replace(",", "."))
        except (TypeError, ValueError):
            odds = None
        line = f"{home} vs {away} - {tip}" + (f" @ {odds:.2f}" if odds else "")
        d = str(t.get("date") or "").strip()
        d = d if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) else None
        key = (d, line.lower())
        if key in seen:
            continue
        seen.add(key)
        error = None
        try:
            parse_tip(line)
        except ValueError as exc:
            error = str(exc)
        if odds is not None and odds <= 1.0:
            error = f"odds {odds:.2f} isn't a real price"
        rows.append({"date": d, "line": line, "error": error})
    unreadable = [str(u) for u in data.get("unreadable") or [] if str(u).strip()]
    return rows, unreadable, summarize_usage(usage)
