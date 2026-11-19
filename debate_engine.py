"""DeBet shared AI plumbing: OpenRouter client, model config, cost tracking, tip parsing and JSON helpers.

The analysis itself lives in value_engine.py; result_verifier.py uses the same client.
(The old 4-round Grok/Claude debate was removed; a copy is in archive/debate_engine_4round.py.)
"""

import json
import os
import re
import time
import urllib.request
from datetime import date, datetime, timezone

from dotenv import load_dotenv
from openai import APIConnectionError, APIStatusError, OpenAI

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Slugs verified live against https://openrouter.ai/api/v1/models on 2026-11-16 (run
# `python debate_engine.py --check-models` to re-check). 'anthropic/claude-sonnet-4' and
# 'x-ai/grok-3' are no longer listed there, so don't fall back to them.
GROK_MODEL = "x-ai/grok-4.7"
GROK_FALLBACK_MODEL = "x-ai/grok-4.6"
CLAUDE_MODEL = "anthropic/claude-sonnet-5.5"
CLAUDE_FALLBACK_MODEL = "~anthropic/claude-sonnet-latest"

# Web search is paid per call, so ONLY the brief searches. _chat() attaches OpenRouter's
# web plugin pinned to the Exa engine (flat ~$0.005/search) when web_search=True. We never
# use the ':online' suffix: on x-ai models it switches to xAI native search, billed far higher.
# _chat() strips any ':online' it sees, so the estimate calls can never trigger a search.
ONLINE_SUFFIX = ":online"
# The brief uses Perplexity Sonar: it searches the web itself and returns a compact answer,
# so it is called with web_search=False (no plugin) - no raw search dump in the prompt.
INTEL_MODEL = "perplexity/sonar"
# If Sonar is unavailable, the brief retries on Grok WITH the Exa web plugin (still one search).
INTEL_FALLBACK_MODEL = GROK_MODEL
WEB_SEARCH_ENGINE = "exa"
WEB_SEARCH_MAX_RESULTS = 3

DEFAULT_MAX_TOKENS = 450          # callers normally pass their own cap
REASONING_EFFORT = "low"          # keeps hidden reasoning tokens (billed as output) small

REQUEST_TIMEOUT = 240             # seconds per call
# OpenRouter answers HTTP 402 "...given your current in-flight requests" when parallel calls
# reserve more credit than the balance. That clears once other calls finish, so wait and retry.
IN_FLIGHT_RETRY_DELAYS = (10, 25)  # seconds

# An analysis costs a few cents. Below this balance we stop before spending anything.
MIN_BALANCE_USD = 0.05
CREDITS_URL = "https://openrouter.ai/settings/credits"

_client = None


class OutOfCreditsError(RuntimeError):
    """OpenRouter answered HTTP 402: the account balance can't cover the request."""


def _get_json(path, timeout=20):
    """GET an OpenRouter API path with the key. Returns parsed JSON or raises."""
    req = urllib.request.Request(OPENROUTER_BASE_URL + path,
                                 headers={"Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY', '')}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def get_credit_balance():
    """Remaining OpenRouter account credit in USD, or None if it can't be read."""
    try:
        data = _get_json("/credits").get("data") or {}
        return float(data["total_credits"]) - float(data["total_usage"])
    except Exception:
        return None


def check_models():
    """{model_id: True/False} - whether each configured model is listed on OpenRouter right now."""
    listed = {m["id"] for m in _get_json("/models", timeout=60).get("data", [])}
    wanted = [GROK_MODEL, GROK_FALLBACK_MODEL, CLAUDE_MODEL, CLAUDE_FALLBACK_MODEL, INTEL_MODEL]
    return {m: m in listed for m in dict.fromkeys(wanted)}


def get_client():
    """Lazily build the OpenRouter client (so importing never needs the key)."""
    global _client
    if _client is None:
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError("OPENROUTER_API_KEY is not set (check your .env file).")
        _client = OpenAI(
            base_url=OPENROUTER_BASE_URL,
            api_key=api_key,
            timeout=REQUEST_TIMEOUT,
            max_retries=2,
            default_headers={"X-Title": "DeBet"},
        )
    return _client


def _search_model(model):
    """Strip any ':online' suffix - web search goes through the Exa plugin instead."""
    return model[: -len(ONLINE_SUFFIX)] if model.endswith(ONLINE_SUFFIX) else model


def _usage_entry(resp, model, label):
    """Token counts and dollar cost from an OpenRouter response's `usage` object."""
    u = getattr(resp, "usage", None)
    extra = (getattr(u, "model_extra", None) or {}) if u is not None else {}
    cost = extra.get("cost")
    try:
        cost = float(cost) if cost is not None else None
    except (TypeError, ValueError):
        cost = None
    return {
        "label": label or "",
        "model": getattr(resp, "model", None) or model,
        "prompt_tokens": int(getattr(u, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(u, "completion_tokens", 0) or 0),
        "cost": cost,
    }


def summarize_usage(calls):
    """Roll up _chat usage entries. total_usd is None if OpenRouter returned no cost at all."""
    costs = [c["cost"] for c in calls if c["cost"] is not None]
    return {
        "calls": list(calls),
        "prompt_tokens": sum(c["prompt_tokens"] for c in calls),
        "completion_tokens": sum(c["completion_tokens"] for c in calls),
        "total_usd": round(sum(costs), 6) if costs else None,
        "cost_complete": len(costs) == len(calls),  # False if some call came back without a cost
    }


def format_cost(summary):
    """'$0.0123 (8,412 in / 1,950 out tokens, 6 calls)'."""
    if not summary or not summary.get("calls"):
        return "$0.0000 (no API calls)"
    usd = summary.get("total_usd")
    price = "cost not returned" if usd is None else f"${usd:.4f}" + ("" if summary.get("cost_complete") else " (partial)")
    return (f"{price} ({summary['prompt_tokens']:,} in / {summary['completion_tokens']:,} out tokens, "
            f"{len(summary['calls'])} calls)")


def _complete(client, model, messages, temperature, max_tokens, extra_body, usage, label):
    """One API call. Records usage, returns (text, finish_reason, model_used)."""
    kwargs = {"model": model, "messages": messages, "max_tokens": max_tokens, "extra_body": extra_body}
    if not model.lstrip("~").startswith("anthropic/"):  # current Claude models reject non-default sampling params
        kwargs["temperature"] = temperature
    resp = client.chat.completions.create(**kwargs)
    if usage is not None:
        usage.append(_usage_entry(resp, model, label))
    # OpenRouter can return HTTP 200 with an error object instead of choices.
    body_error = (getattr(resp, "model_extra", None) or {}).get("error")
    if body_error:
        raise RuntimeError(f"OpenRouter error: {body_error.get('message', body_error) if isinstance(body_error, dict) else body_error}")
    choice = resp.choices[0] if resp.choices else None
    text = (choice.message.content or "").strip() if choice else ""
    reason = getattr(choice, "finish_reason", None) if choice else "no choices returned"
    return text, reason, resp.model or model


def _chat(models, messages, web_search=False, temperature=0.4, max_tokens=DEFAULT_MAX_TOKENS,
          usage=None, label=None):
    """Call the first model that works. Returns (text, model_used).

    `models` is a model id or a list tried in order; a 4xx/5xx or connection
    error on one model moves on to the next. Web search runs only when
    `web_search=True`. If `usage` is a list, one usage/cost entry per API call is appended.
    """
    models = [_search_model(m) for m in ([models] if isinstance(models, str) else models)]
    extra_body = {"usage": {"include": True}, "reasoning": {"effort": REASONING_EFFORT, "exclude": True}}
    if web_search:
        extra_body["plugins"] = [{"id": "web", "engine": WEB_SEARCH_ENGINE,
                                  "max_results": WEB_SEARCH_MAX_RESULTS}]
    client = get_client()  # a missing API key is reported as-is, not as a model failure
    errors, last_exc = [], None
    for model in models:
        for attempt in range(len(IN_FLIGHT_RETRY_DELAYS) + 1):
            try:
                text, reason, used = _complete(client, model, messages, temperature, max_tokens,
                                               extra_body, usage, label)
                if not text and reason == "length":
                    # Hidden reasoning used up the whole cap - retry once with headroom.
                    text, reason, used = _complete(client, model, messages, temperature, max_tokens * 2,
                                                   extra_body, usage, label)
                if not text:
                    raise RuntimeError(f"Empty response (finish_reason={reason})")
                return text, used
            except (APIStatusError, APIConnectionError, RuntimeError) as exc:
                detail = _describe_error(exc)
                last_exc = exc
                if _is_in_flight_402(exc) and attempt < len(IN_FLIGHT_RETRY_DELAYS):
                    wait = IN_FLIGHT_RETRY_DELAYS[attempt]
                    print(f"[DeBet API ERROR] {label or 'call'} · {model}: {detail} - retrying in {wait}s",
                          flush=True)
                    time.sleep(wait)
                    continue
                print(f"[DeBet API ERROR] {label or 'call'} · {model}: {detail}", flush=True)
                errors.append(f"{model}: {detail}")
                if isinstance(exc, APIStatusError) and exc.status_code == 402:
                    # Out of credit: every fallback model would fail the same way, so stop here.
                    balance = get_credit_balance()
                    hint = (f"OpenRouter balance ${balance:.2f}" if balance is not None else "OpenRouter balance too low")
                    raise OutOfCreditsError(f"OpenRouter call failed - {' | '.join(errors)} "
                                            f"[{hint} - top up at {CREDITS_URL}]") from exc
                break
    raise RuntimeError("OpenRouter call failed - " + " | ".join(errors)) from last_exc


def _is_in_flight_402(exc):
    return (isinstance(exc, APIStatusError) and exc.status_code == 402
            and "in-flight" in _describe_error(exc).lower())


def _describe_error(exc):
    """Readable one-liner for an OpenRouter/OpenAI SDK exception, including HTTP status and message."""
    if isinstance(exc, APIStatusError):
        msg = exc.message
        body = exc.body if isinstance(exc.body, dict) else {}
        inner = body.get("error", body) if isinstance(body.get("error", body), dict) else {}
        detail = inner.get("message") or msg
        raw = (inner.get("metadata") or {}).get("raw")
        raw = str(raw)[:300] if raw else None
        return f"HTTP {exc.status_code} - {detail}" + (f" ({raw})" if raw and raw not in detail else "")
    if isinstance(exc, APIConnectionError):
        return f"connection error - {exc.__cause__ or exc}"
    return str(exc)


# ---------------------------------------------------------------------------
# Tip parsing
# ---------------------------------------------------------------------------
# Accepts free-form lines such as:
#   Kazakhstan Vs Slovakia over 2.5 1.95
#   Tenerife vs Ceuta - BTTS Yes @ 1.85
#   Arsenal vs Chelsea 1X 1.45
#   Arsenal v Chelsea | Chelsea to win | odds 3.10
_VS_RE = re.compile(r"\s+(?:vs\.?|v\.?|versus)\s+", re.IGNORECASE)
# Explicit match/market separators (spaced so "Saint-Germain" and "2-1" survive).
_SEP_RE = re.compile(r"\s+[-|:]\s+|\s*[|–—]\s*|:\s+|,\s+|\s+-(?=\s*[A-Za-z])")
# Odds marked explicitly: "@ 1.85", "odds 1.85", "odds: 1.85", "at 1.85".
_MARKED_ODDS_RE = re.compile(r"(?:@|\bodds\b\s*:?|\bat\b)\s*\(?(\d+(?:[.,]\d+)?)\)?\s*$", re.IGNORECASE)
# Unmarked trailing decimal: "... over 2.5 1.95" or "... BTTS (1.85)".
_TRAILING_ODDS_RE = re.compile(r"(?:^|\s)\(?(\d{1,3}[.,]\d{1,3})\)?\s*$")
# Words after which a number is a goal/handicap line, not odds ("over 2.5").
_LINE_WORDS = {"over", "under", "ov", "un", "o", "u", "ah", "handicap", "hcp", "line", "total", "totals"}
_MARKET_WORDS = {
    "over", "under", "ov", "un", "btts", "bts", "gg", "ng", "both", "1x", "x2", "12", "1", "2", "x",
    "dnb", "draw", "home", "away", "ah", "handicap", "hcp", "double", "dc", "win", "corners", "cards",
    "clean", "correct", "cs", "ht", "ft", "1h", "2h", "1st", "2nd", "total", "totals", "asian",
    "either", "1x2", "ht/ft", "winner", "to",
}
_LINE_TOKEN_RE = re.compile(r"^(?:[ou]\d+(?:[.,]\d+)?|[+-]\d+(?:[.,]\d+)?|\d+[-:]\d+)$", re.IGNORECASE)


def _num(value):
    return value.replace(",", ".")


def _extract_odds(text):
    """Pull decimal odds off the end of the line. Returns (odds or None, text_without_odds)."""
    m = _MARKED_ODDS_RE.search(text)
    if not m:
        m = _TRAILING_ODDS_RE.search(text)
        if m:
            before = text[: m.start()].split()
            # "over 2.5" with no odds: the number is the goal line, not a price.
            if before and before[-1].lower().strip("(") in _LINE_WORDS:
                return None, text
    if not m:
        return None, text
    return float(_num(m.group(1))), text[: m.start()].rstrip(" -@:|,–—(")


def _is_market_start(token):
    t = token.lower().strip("()[],")
    return t in _MARKET_WORDS or bool(_LINE_TOKEN_RE.match(t))


def _split_away_and_market(rest, home):
    """Split 'Slovakia over 2.5' into ('Slovakia', 'over 2.5')."""
    sep = _SEP_RE.search(rest)
    if sep and sep.start() > 0:
        away, market = rest[: sep.start()], rest[sep.end():]
    else:
        tokens = rest.split()
        idx = next((i for i in range(1, len(tokens)) if _is_market_start(tokens[i])), None)
        if idx is None:
            raise ValueError("Couldn't tell where the away team ends and the market begins - "
                             "add ' - ' between them, e.g. 'Arsenal vs Chelsea - Over 2.5 @ 1.90'.")
        away, market = " ".join(tokens[:idx]), " ".join(tokens[idx:])
    # A market that names a team ("Chelsea Arsenal to win", "Chelsea Chelsea DNB")
    # leaves that team glued onto the away side - move it back to the market.
    words = away.split()
    for k in range(1, len(words)):
        head, tail = words[:k], words[k:]
        tail_s = " ".join(tail).lower()
        if tail_s == home.lower() or [w.lower() for w in head] == [w.lower() for w in tail]:
            away, market = " ".join(head), f"{' '.join(tail)} {market}"
            break
    return away.strip(), market.strip(" -@:|,–—")


def _normalize_market(market, home, away):
    """Turn shorthand ('over 2.5', 'o2.5', 'gg', '1x', '2') into a clean market label."""
    s = re.sub(r"\s+", " ", market).strip()
    low = s.lower()

    m = re.fullmatch(r"(over|ov|o|under|un|u)\s*(\d+(?:[.,]\d+)?)\s*(goals?|corners?|cards?|bookings?|points?)?\s*(.*)", low)
    if m:
        side = "Over" if m.group(1).startswith("o") else "Under"
        noun = (m.group(3) or "goals").rstrip("s").capitalize() + "s"
        tail = s[len(s) - len(m.group(4)):] if m.group(4) else ""
        return f"{side} {_num(m.group(2))} {noun}" + (f" ({tail})" if tail else "")

    m = re.fullmatch(r"(?:btts|bts|gg|both teams to score)\s*[:\-]?\s*(yes|y|no|n)?", low)
    if m:
        return "BTTS No" if (m.group(1) or "yes").startswith("n") else "BTTS Yes"
    if low in ("ng", "no goal", "btts no"):
        return "BTTS No"

    m = re.fullmatch(r"(?:dc|double chance)?\s*(1x|x2|12)", low)
    if m:
        return f"Double Chance {m.group(1).upper()}"
    if low in ("1", "home", "home win"):
        return f"{home} to Win"
    if low in ("2", "away", "away win"):
        return f"{away} to Win"
    if low in ("x", "draw"):
        return "Draw"
    if low == "dnb":
        return "Draw No Bet"
    m = re.fullmatch(r"(?:cs|correct score)?\s*(\d+)\s*[-:]\s*(\d+)", low)
    if m:
        return f"Correct Score {m.group(1)}-{m.group(2)}"

    # Anything else: keep the wording, tidy common abbreviations.
    s = re.sub(r"\b(btts|dnb|ht|ft|ah|1x2)\b", lambda mm: mm.group(1).upper(), s, flags=re.IGNORECASE)
    return s[:1].upper() + s[1:]


def parse_tip(tip_text):
    """Parse a free-form tip line into match_name / market / odds (+ teams, implied probability).

    'Kazakhstan Vs Slovakia over 2.5 1.95' ->
        match_name 'Kazakhstan vs Slovakia', market 'Over 2.5 Goals', odds 1.95
    """
    text = re.sub(r"\s+", " ", (tip_text or "")).strip()
    if not text:
        raise ValueError("Tip text is empty.")
    odds, body = _extract_odds(text)
    if odds is not None and not 1.0 < odds <= 1000:
        raise ValueError(f"Odds {odds} look wrong - use decimal odds greater than 1.00.")

    vs = _VS_RE.search(body)
    if not vs:
        raise ValueError("Couldn't find the two teams - write them as 'Home vs Away'.")
    home = body[: vs.start()].strip(" -")
    away, market_raw = _split_away_and_market(body[vs.end():].strip(), home)
    if not (home and away):
        raise ValueError("Couldn't read both team names - write them as 'Home vs Away'.")
    if not market_raw:
        raise ValueError("No market found - add the bet, e.g. 'Over 2.5', 'BTTS Yes', '1X'.")

    market = _normalize_market(market_raw, home, away)
    return {
        "raw": text,
        "match_name": f"{home} vs {away}",
        "home_team": home,
        "away_team": away,
        "market": market,
        "odds": odds,
        "implied_probability": round(100.0 / odds, 1) if odds else None,
    }


DATE_DRIFT_WARNING_DAYS = 3  # warn when the confirmed date is this far from the admin's date


def resolve_match_date(meta, default_date=None, today=None):
    """Pick the match date to store: the fixture date Step 0 confirmed, else the admin's date-picker.

    A "confirmed" date more than a day before `today` is treated as a past head-to-head picked up
    by mistake (tips are for upcoming matches) and ignored.
    Returns (match_date 'YYYY-MM-DD' or None, source label, warning or None).
    """
    default = default_date.isoformat() if isinstance(default_date, date) else (default_date or None)
    today = today or datetime.now(timezone.utc).date()
    raw = str((meta or {}).get("match_date") or "").strip()
    found = None
    m = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
    if m:
        try:
            found = date.fromisoformat(m.group(1))
        except ValueError:
            found = None
    confirmed = (meta or {}).get("match_date_confirmed")
    confirmed = confirmed is True or str(confirmed).strip().lower() == "true"
    stale = found is not None and (today - found).days > 1
    if stale and default:
        return default, "admin date-picker", (f"Step 0 returned {found.isoformat()}, which is in the past "
                                              f"(likely an old meeting) - using your Match Date ({default}).")

    if found and confirmed and not stale:
        source = "live fixture data" + (f" ({meta['fixture_source']})" if (meta or {}).get("fixture_source") else "")
        warning = None
        if default:
            drift = abs((found - date.fromisoformat(default)).days)
            if drift > DATE_DRIFT_WARNING_DAYS:
                warning = (f"Fixture data puts this match on {found.isoformat()}, {drift} days from the "
                           f"date you picked ({default}). Check it's the right fixture before publishing.")
        return found.isoformat(), source, warning
    if default:
        why = f"Step 0 found {found.isoformat()} but could not confirm it" if found else "Step 0 could not confirm the date"
        return default, "admin date-picker", f"{why} - using your Match Date ({default})."
    return (found.isoformat() if found else None), ("unconfirmed AI estimate" if found else "unknown"), None

# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------
def _extract_json_line(text, label):
    """Find 'LABEL: {...}' (possibly fenced) and parse it. Returns (dict or None, text_without_line)."""
    m = re.search(rf"{label}\s*:\s*`*\s*(\{{.*\}})\s*`*\s*$", text, re.IGNORECASE | re.DOTALL | re.MULTILINE)
    if not m:
        return None, text
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None, text
    return data, text[: m.start()].rstrip()


_META_LINE_RE = re.compile(r"^[\s*_>`]*META\s*:\s*`*\s*(\{[^\n]*\})\s*`*\s*$", re.IGNORECASE | re.MULTILINE)


def _extract_meta(text):
    """Pull the single-line 'META: {...}' out of the brief, wherever it sits (asked for first,
    so a brief cut off by max_tokens still keeps it). Returns (dict or None, brief_without_line)."""
    m = _META_LINE_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1)), (text[: m.start()] + text[m.end():]).strip()
        except json.JSONDecodeError:
            pass
    return _extract_json_line(text, "META")


def _parse_json_object(text):
    """Strictly parse a model's JSON reply. Tolerates only a code fence / 'FINAL_JSON:' prefix or a
    stray word around the object - the content itself must be valid JSON. Returns dict or None."""
    s = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", str(text or "").strip(), flags=re.IGNORECASE)
    s = re.sub(r"^\s*FINAL_JSON\s*:\s*", "", s, flags=re.IGNORECASE)
    candidates = [s]
    start, end = s.find("{"), s.rfind("}")
    if 0 <= start < end:
        candidates.append(s[start:end + 1])
    for c in candidates:
        try:
            data = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


if __name__ == "__main__":
    # python debate_engine.py --check-models   -> are the configured OpenRouter models still listed?
    for model_id, ok in check_models().items():
        print(f"{'OK     ' if ok else 'MISSING'} {model_id}")
    bal = get_credit_balance()
    print("OpenRouter balance:", f"${bal:.2f}" if bal is not None else "unknown")
