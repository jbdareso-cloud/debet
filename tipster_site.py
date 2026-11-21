"""Read daily tips from public tipster pages - free, no AI. TaifaTips (4 tips a day).

One page request per button press, for personal use in this private app (nothing is republished).
Returns rows in the same shape as screenshot_reader.read_tips, so the Analyse tab can fill the slots
the same way: [{"date": 'YYYY-MM-DD', "line": 'Home vs Away - Tip @ 1.51', "error": None or str}].
"""

import gzip
import re
import urllib.request
from datetime import date
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
        raise RuntimeError(f"Couldn't open {site} ({exc}). Try again later.") from exc


def read_tips():
    """TaifaTips: fetch the page once and return (rows, problems). Raises RuntimeError if it can't be read."""
    rows = parse_page(_fetch(URL, "the tips page"))
    if not rows:
        raise RuntimeError("The tips page opened, but no tips table was found - its layout may have changed.")
    return rows, []
