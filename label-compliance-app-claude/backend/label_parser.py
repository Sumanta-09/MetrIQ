"""
label_parser.py

Shared parsing helpers for turning raw OCR text from a product label
into typed, labelled values — dates, prices, quantities — that the
compliance engine can reason about.

Both ocr_engine (extraction) and compliance_engine (judgement) import
from here, so a date string is parsed exactly the same way no matter
which side needs it.

Supported date styles (Indian packaging conventions):
  dd/mm/yy, dd/mm/yyyy, dd-mm-yy, dd.mm.yyyy      -> 30/07/26
  mm/yyyy, mm.yy, mm-yyyy                          -> 07/2026 (month & year, Rule 6(1)(d))
  yyyy-mm-dd (ISO, printed on some labels)         -> 2026-07-30
  "JUL 2026" / "JULY 2026" month-name styles      -> JUL 2026
  "MAY 2026 & NOV 2026" combo declarations
  relative best-before                             -> "Best before 9 months from packaging"
  packed-on style                                  -> "PKD 07/2026"
"""

import re
from datetime import datetime, timedelta

MONTH_NAMES = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "SEPT": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

# "07/2026", "07-2026", "07.2026", "07/26" — month & year, Rule 6(1)(d) style.
_MONTH_YEAR_RE = r'(?<!\d)(0?[1-9]|1[0-2])\s*[/\-.]\s*((?:20)?\d{2})(?!\d)'
# dd/mm/yyyy-ish full dates (any separator).
_FULL_DATE_RE = r'(?<!\d)(\d{1,2})\s*[/\-.]\s*(\d{1,2})\s*[/\-.]\s*((?:20)?\d{2})(?!\d)'
# "JUL 2026" / "JULY 2026" / "JUL-2026".
_MONTH_NAME_RE = (
    r'(?<![A-Z])(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|SEPT|OCT|NOV|DEC)[A-Z]{0,6}'
    r'[\s\-,\.]*((?:20)?\d{2})(?![A-Z0-9])'
)


def parse_date(value):
    """
    Parses a label date string into a datetime, or None.

    Handles full dates (dd/mm/yy[yy], yyyy-mm-dd), month-year
    declarations (mm/yyyy -> 1st of month, "JUL 2026" -> 1st of month),
    and bare years (interpreted as the December rule of LMPC Rule 6:
    a "2026" date mark means the product is valid through the end of
    2026 — see the 'period_end' concept in parse_date_mark).
    """
    if not value:
        return None
    s = str(value).strip().upper()
    s = re.sub(r'\s+', ' ', s)

    # ISO yyyy-mm-dd
    m = re.fullmatch(r'(\d{4})-(\d{1,2})-(\d{1,2})', s)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    # Month name, e.g. "JUL 2026" / "SEPTEMBER 2026"
    m = re.fullmatch(rf'{_MONTH_NAME_RE}', s)
    if m:
        month, year = MONTH_NAMES[m.group(1)], int(m.group(2))
        try:
            return datetime(2000 + year if year < 100 else year, month, 1)
        except ValueError:
            return None

    # Month & year, e.g. "07/2026", "7.26" (treated as 1st of the month)
    m = re.fullmatch(rf'{_MONTH_YEAR_RE}', s)
    if m:
        month = int(m.group(1))
        year = int(m.group(2))
        year = 2000 + year if year < 100 else year
        try:
            return datetime(year, month, 1)
        except ValueError:
            return None

    # Full date dd/mm/yy(yy) with -, . or / separators.
    m = re.fullmatch(rf'{_FULL_DATE_RE}', s)
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        year = 2000 + year if year < 100 else year
        try:
            return datetime(year, month, day)
        except ValueError:
            return None

    return None


def parse_date_mark(value):
    """
    Like parse_date, but also understands bare years ("2026") and
    returns the *period* a date mark legally covers, since Rule 6(1)(d)
    only demands month & year:

        {"start": datetime, "end": datetime}  (end inclusive of that period)
        None                                  (unparseable)

    For "07/2026" the mark covers 01-31 July 2026; for "2026" it covers
    the whole year (Rule 6(1)(d) proviso: a bare year means valid
    until 31 December of that year).
    """
    if not value:
        return None
    s = str(value).strip().upper()

    dt = parse_date(s)
    if dt:
        # Which granularity did the source string express?
        if re.fullmatch(rf'{_MONTH_YEAR_RE}', s) or re.fullmatch(rf'{_MONTH_NAME_RE}', s):
            start = dt.replace(day=1)
            return {"start": start, "end": _end_of_month(start)}
        # Full date: the mark covers just that day.
        return {"start": dt, "end": dt}

    # Bare 4-digit year -> 1 Jan .. 31 Dec of that year.
    m = re.fullmatch(r'(20\d{2})', s)
    if m:
        year = int(m.group(1))
        return {"start": datetime(year, 1, 1), "end": datetime(year, 12, 31)}

    return None


def _end_of_month(dt):
    if dt.month == 12:
        return datetime(dt.year, 12, 31)
    return datetime(dt.year, dt.month + 1, 1) - timedelta(days=1)


def date_mark_covers(mark, when):
    """True if the period covered by a parsed date mark includes `when`."""
    if not mark:
        return False
    return mark["start"] <= when <= mark["end"]


def relative_best_before_months(text):
    """
    Extracts a shelf-life length in months from a relative best-before
    declaration, e.g. "Best before 9 months from packaging" -> 9.
    Returns (months:int|None, raw_text:str|None). Days/years are
    converted to fractional months.
    """
    if not text:
        return None, None
    m = re.search(
        r'best\s*before(?:\s*[^\d]{0,40}?|\s*:?\s*)'
        r'(\d+(?:\.\d+)?)\s*(day|days|month|months|year|years)',
        text, re.IGNORECASE
    )
    if not m:
        return None, None
    n = float(m.group(1))
    unit = m.group(2).lower()
    if unit.startswith("day"):
        months = n / 30.0
    elif unit.startswith("year"):
        months = n * 12.0
    else:
        months = n
    return months, m.group(0).strip()


# ----------------------------------------------------------------------
# Prices
# ----------------------------------------------------------------------
def parse_price(raw):
    """
    Parses a price string into {"amount": float, "currency": str,
    "raw": str} or None. Handles ₹ / Rs / Rs. / INR / $ / € / £
    prefixes, Indian digit grouping (1,299.00 / 1,29,999), trailing
    "/-" and unicode digits.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None

    m = re.match(
        r'^\s*(₹|rs\.?|inr|\$|€|£)?\s*'
        r'(\d[\d,\u0966-\u096F]*(?:\.\d{1,2})?)'   # grouped digits, optional paise
        r'\s*(?:/-|/-\.|-/-)?\s*$',
        s, re.IGNORECASE
    )
    if not m:
        return None

    currency = (m.group(1) or "").strip().rstrip(".").upper()
    digits = m.group(2)
    if currency in ("RS", "INR"):
        currency = "INR"
    elif currency in ("$",):
        currency = "USD"
    elif currency in ("€",):
        currency = "EUR"
    elif currency in ("£",):
        currency = "GBP"
    elif currency in ("₹", ""):
        currency = "INR"

    try:
        amount = float(digits.replace(",", ""))
    except ValueError:
        return None
    return {"amount": amount, "currency": currency, "raw": s}


def format_price(parsed):
    """Formats a parse_price result back to a display string."""
    if not parsed:
        return ""
    currency = {"INR": "Rs.", "USD": "$", "EUR": "€", "GBP": "£"}.get(
        parsed["currency"], parsed["currency"])
    amount = parsed["amount"]
    text = f"{amount:,.2f}".rstrip("0").rstrip(".")
    return f"{currency} {text}"
