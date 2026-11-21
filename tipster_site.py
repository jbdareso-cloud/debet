"""Read daily tips from public tipster pages - free, no AI. TaifaTips (4 tips) and BetMines (Daily Bets).

One page request per button press, for personal use in this private app (nothing is republished).
Returns rows in the same shape as screenshot_reader.read_tips, so the Analyse tab can fill the slots
the same way: [{"date": 'YYYY-MM-DD', "line": 'Home vs Away - Tip @ 1.51', "error": None or str}].
"""

import gzip
import re
import urllib.request
from datetime import date
from html import unescape
from html.parser import HTMLParser

from debate_engine import parse_tip
from screenshot_reader import _clean_tip

URL = "https://taifatips.com/"
USER_AGENT = "Mozilla/5.0 (compatible; DeBet personal tip reader)"
MAX_DAYS = 3  # the page also lists older days; only the newest few are useful for filling slots


class _FirstTable(HTMLParser):
    """Collects the rows of the first table on the page as lists of cell texts."""

    def __init__(self):
        super().__init__()
        self.rows, self._row, self._cell = [], None, None
        self.done = False

    def handle_starttag(self, tag, attrs):
        if self.done:
            return
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if self.done:
            return
        if tag in ("td", "th") and self._row is not None and self._cell is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None
        elif tag == "table" and self.rows:
            self.done = True

    def handle_data(self, data):
        if self._cell is not None and not self.done:
            self._cell.append(data)


def _day(text):
    """'Saturday 21.11.26' -> '2026-11-21', else None."""
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{2})\b", text or "")
    if not m:
        return None
    try:
        return date(2000 + int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
    except ValueError:
        return None


def parse_page(page_html):
    """Tip rows from the page's first table, newest days first."""
    table = _FirstTable()
    table.feed(page_html)
    rows, current, days = [], None, []
    for cells in table.rows:
        texts = [c for c in cells if c]
        if len(texts) == 1 and _day(texts[0]):          # date header row
            current = _day(texts[0])
            if current not in days:
                days.append(current)
            continue
        if current is None or len(cells) < 4 or " vs " not in cells[1]:
            continue                                     # column titles, spacers, anything unexpected
        match, tip, odds_text = cells[1], _clean_tip(cells[2]), cells[3]
        try:
            odds = float(odds_text.replace(",", "."))
        except ValueError:
            odds = None
        line = f"{match} - {tip}" + (f" @ {odds:.2f}" if odds else "")
        error = None
        try:
            parse_tip(line)
        except ValueError as exc:
            error = str(exc)
        if odds is not None and odds <= 1.0:
            error = f"odds {odds:.2f} isn't a real price"
        rows.append({"date": current, "line": line, "error": error})
    keep = set(days[:MAX_DAYS])
    return [r for r in rows if r["date"] in keep]


def _fetch(url, site):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip",
                                               "Accept-Language": "en-GB,en;q=0.8"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":  # ~4x smaller download
                raw = gzip.decompress(raw)
            return raw.decode("utf-8", errors="replace")
    except Exception as exc:
        raise RuntimeError(f"Couldn't open {site} ({exc}). Use a screenshot instead.") from exc


def _row(date_iso, line):
    error = None
    try:
        parse_tip(line)
    except ValueError as exc:
        error = str(exc)
    m = re.search(r"@ (\d+(?:\.\d+)?)$", line)
    if m and float(m.group(1)) <= 1.0:
        error = f"odds {m.group(1)} isn't a real price"
    return {"date": date_iso, "line": line, "error": error}


def read_tips():
    """TaifaTips: fetch the page once and return (rows, problems). Raises RuntimeError if it can't be read."""
    rows = parse_page(_fetch(URL, "TaifaTips"))
    if not rows:
        raise RuntimeError("TaifaTips opened, but no tips table was found - the page layout may have "
                           "changed. Use a screenshot instead.")
    return rows, []


# --- BetMines "Daily Bets" (a Double and a Risk, two tips each) ----------------------------------------
# Their terms allow copying content "for your own internal, non-commercial, lawful use".
BETMINES_URL = "https://betmines.com/daily-bets-football"
_MONTHS = {  # month names in the languages the page has shown dates in
    **{m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                   "september", "october", "november", "december"], 1)},
    **{m: i for i, m in enumerate(["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto",
                                   "setembro", "outubro", "novembro", "dezembro"], 1)},
    **{m: i for i, m in enumerate(["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
                                   "septiembre", "octubre", "noviembre", "diciembre"], 1)},
    **{m: i for i, m in enumerate(["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio",
                                   "agosto", "settembre", "ottobre", "novembre", "dicembre"], 1)},
}


def _long_date(text):
    """'21 de novembro de 2026' / '21 November 2026' -> '2026-11-21', else None."""
    m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s+(?:de\s+)?([^\W\d_]+)\s+(?:de\s+)?(\d{4})", text or "", re.UNICODE)
    month = _MONTHS.get(m.group(2).lower()) if m else None
    try:
        return date(int(m.group(3)), month, int(m.group(1))).isoformat() if month else None
    except ValueError:
        return None


def _betmines_tip(market, pick):
    """BetMines' market + pick -> a tip parse_tip understands, or None if it's a market DeBet doesn't use."""
    market, pick = market.strip().lower(), pick.strip()
    m = re.fullmatch(r"([+-])\s*(\d\.5)", pick)
    if market in ("number of goals", "goals", "under/over") and m:
        return ("Over " if m.group(1) == "+" else "Under ") + m.group(2)
    if market == "1x2" and pick.upper() in ("1", "X", "2"):
        return pick.upper()
    if market in ("double chance",) and pick.upper() in ("1X", "X2", "12"):
        return pick.upper()
    if market in ("both teams to score", "btts", "goal/no goal") and pick.lower() in ("yes", "no", "gg", "ng"):
        return "BTTS Yes" if pick.lower() in ("yes", "gg") else "BTTS No"
    return None


def parse_betmines(page_html, today=None):
    """Tip rows from the Daily Bets section (Double, then Risk)."""
    start = re.search(r">\s*Double\s*</h2>", page_html)
    end = page_html.find("How do we pick", start.end() if start else 0)
    if not start:
        return []
    block = page_html[start.start():end if end > 0 else len(page_html)]
    day = _long_date(re.sub(r"<[^>]+>", " ", block[:2000])) or (today or date.today()).isoformat()
    rows = []
    for a in re.findall(r'(?s)<a[^>]+href="/matches/predictions-[^"]*"[^>]*>(.*?)</a>', block):
        texts = [re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", "", p))).strip()
                 for p in re.findall(r"(?s)<p[^>]*>(.*?)</p>", a)]
        texts = [t for t in texts if t]
        if len(texts) < 5:
            continue
        home, away, market, pick, odds_text = texts[0], texts[1], texts[-3], texts[-2], texts[-1]
        tip = _betmines_tip(market, pick)
        line = f"{home} vs {away} - {tip or f'{market} {pick}'} @ {odds_text}"
        row = _row(day, line)
        if not tip:
            row["error"] = f"market '{market} {pick}' isn't one DeBet analyses"
        rows.append(row)
    return rows


def read_betmines(today=None):
    """BetMines Daily Bets: (rows, problems). Raises RuntimeError if the page can't be read."""
    rows = parse_betmines(_fetch(BETMINES_URL, "BetMines"), today)
    if not rows:
        raise RuntimeError("BetMines opened, but no daily bets were found - they may not be posted yet, or "
                           "the page layout changed.")
    return rows, []
