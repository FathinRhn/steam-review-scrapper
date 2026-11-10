"""Shared helpers used by the scraper, cleaner, and validator."""

from __future__ import annotations

import calendar
import hashlib
import html
import json
import logging
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import config


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def setup_logging(name: str = "delta_force_scraper") -> logging.Logger:
    """Return a logger that writes to both the console and reports/scrape_log.txt."""
    config.ensure_directories()

    # Windows consoles default to cp1252; force UTF-8 so review text never
    # crashes a log statement.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    logger = logging.getLogger(name)
    if logger.handlers:  # already configured (e.g. imported by a script)
        return logger

    logger.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    file_handler = logging.FileHandler(config.LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    logger.propagate = False
    return logger


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Files / state
# ---------------------------------------------------------------------------
def load_json(path: Path, default: Any) -> Any:
    """Load a JSON file, returning *default* when it is missing or corrupt."""
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError):
        return default


def atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON to a temporary file first, then replace the target.

    This prevents a half-written state file if the process is interrupted
    exactly while saving.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def polite_sleep(minimum: float | None = None, maximum: float | None = None) -> None:
    """Sleep for a random interval between requests."""
    import random

    low = config.MIN_DELAY if minimum is None else minimum
    high = config.MAX_DELAY if maximum is None else maximum
    time.sleep(random.uniform(low, high))


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------
_BBCODE_RE = re.compile(
    r"\[/?(?:"
    r"b|i|u|s|strike|spoiler|noparse|code|h[1-6]|list|\*|olist|quote|"
    r"url|img|video|previewyoutube|table|tr|td|th|hr|p|center|left|right|"
    r"size|color|font"
    r")(?:=[^\]]*)?\]",
    re.IGNORECASE,
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\ufeff]")


def clean_review_text(text: str | None) -> str:
    """Convert Steam BBCode/HTML into readable plain text.

    The original review text is always kept in a separate column; this function
    only produces the analysis-friendly version.
    """
    if not text:
        return ""

    value = html.unescape(str(text))
    value = _ZERO_WIDTH_RE.sub("", value)
    value = _BBCODE_RE.sub(" ", value)
    value = _HTML_TAG_RE.sub(" ", value)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t\f\v]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def to_int(value: Any, default: int | None = 0) -> int | None:
    """Best-effort integer conversion for HTML strings like '1,234'."""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    cleaned = re.sub(r"[^\d-]", "", str(value))
    if cleaned in ("", "-"):
        return default
    try:
        return int(cleaned)
    except ValueError:
        return default


def to_float(value: Any, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[^\d.,-]", "", str(value)).replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return default


def safe_bool(value: Any) -> bool | None:
    """Preserve missing values instead of silently turning them into False."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def sha256_short(value: Any, length: int = 16) -> str | None:
    if value is None or value == "":
        return None
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()
    return digest[:length]


# ---------------------------------------------------------------------------
# Steam "Posted: 9 November" date parsing (HTML fallback only)
# ---------------------------------------------------------------------------
_MONTHS: dict[str, int] = {}
for _index in range(1, 13):
    _MONTHS[calendar.month_name[_index].lower()] = _index
    _MONTHS[calendar.month_abbr[_index].lower()] = _index


def parse_posted_date(
    posted_text: str | None, reference: datetime | None = None
) -> tuple[datetime | None, str]:
    """Parse the relative date shown on community review cards.

    Examples handled:
        "Posted: 9 November"
        "Posted: November 9"
        "Posted: 12 December, 2024"
        "Posted: 9 Nov, 2024"
        "Posted: Today" / "Posted: Yesterday"

    Returns ``(datetime_utc, precision)`` where precision is one of
    ``exact_day``, ``inferred_year``, ``missing``, or ``unparsed``.

    Steam hides the year for reviews younger than one year, so the year is
    inferred from the scrape date.  This is accurate to the day, which is
    enough for trend analysis; exact API timestamps are used whenever
    available.
    """
    if not posted_text:
        return None, "missing"

    reference = reference or utc_now()
    text = re.sub(r"(?i)^\s*posted:\s*", "", str(posted_text)).strip()
    text = re.sub(r"\s+", " ", text).strip(" ,")
    lowered = text.lower()

    if lowered in {"today", "just now"}:
        return reference.replace(hour=0, minute=0, second=0, microsecond=0), "inferred_year"
    if lowered == "yesterday":
        day = reference - timedelta(days=1)
        return day.replace(hour=0, minute=0, second=0, microsecond=0), "inferred_year"

    day: int | None = None
    month: int | None = None
    year: int | None = None

    # "9 November" / "9 November, 2024"
    match = re.search(r"(?i)\b(\d{1,2})\s+([A-Za-z]+)(?:,?\s+(\d{4}))?\b", text)
    if match:
        day = int(match.group(1))
        month = _MONTHS.get(match.group(2).lower())
        year = int(match.group(3)) if match.group(3) else None

    # "November 9" / "November 9, 2024"
    if month is None:
        match = re.search(r"(?i)\b([A-Za-z]+)\s+(\d{1,2})(?:,?\s+(\d{4}))?\b", text)
        if match:
            month = _MONTHS.get(match.group(1).lower())
            day = int(match.group(2))
            year = int(match.group(3)) if match.group(3) else None

    if month is None or day is None:
        return None, "unparsed"

    explicit_year = year is not None
    if year is None:
        year = reference.year

    try:
        parsed = datetime(year, month, day, tzinfo=timezone.utc)
    except ValueError:
        return None, "unparsed"

    # A review cannot be dated in the future.  Steam shows no year for recent
    # reviews, so a "future" month/day must belong to the previous year.
    if not explicit_year and parsed > reference + timedelta(days=1):
        try:
            parsed = parsed.replace(year=year - 1)
        except ValueError:  # 29 February
            parsed = parsed.replace(year=year - 1, day=28)

    return parsed, ("exact_day" if explicit_year else "inferred_year")
